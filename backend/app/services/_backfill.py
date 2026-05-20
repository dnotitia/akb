"""Shared runtime for background backfill workers.

Both `embed_worker` (embedding API retries) and `delete_worker` (vector-store
upsert + delete outbox) share the same loop shape:

- periodically claim a batch with `FOR UPDATE SKIP LOCKED`,
- process it,
- on idle sleep with early-wake on stop, on work drain aggressively.

They also share the same exponential backoff schedule (60s → 6h, cap 8
retries). This module factors both out so the worker modules only need
to implement the batch processor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

# Retry backoff shared by every backfill worker. Index is retry_count (0-based).
# After MAX_RETRIES the row stays in 'abandoned' until operator intervention.
BACKOFF_SECS: list[int] = [60, 300, 900, 1800, 3600, 7200, 14400, 21600]
MAX_RETRIES: int = len(BACKOFF_SECS)
# Idle wake interval. Lower = freshly-written content shows up in dense
# search faster; higher = fewer no-op DB pings. 10s strikes a middle:
# new docs become searchable within ~20s worst case (embed_worker tick +
# delete_worker tick) instead of two-minute lag, while still costing
# only a handful of trivial PG queries per minute across all workers.
IDLE_INTERVAL_SECS: int = 10


def next_attempt_delay(retry_count: int) -> int:
    return BACKOFF_SECS[min(retry_count, len(BACKOFF_SECS) - 1)]


class BackfillRunner:
    """Owns the asyncio task lifecycle for one or more backfill worker tasks.

    The caller supplies `process_once`, an async callable returning the
    number of items processed. We handle the idle/drain cadence and
    graceful stop.

    Set `concurrency > 1` to spawn that many sibling tasks against the
    same queue. Workers coordinate at the DB layer (FOR UPDATE SKIP
    LOCKED), so they will not race on the same row. Task names get an
    index suffix so per-worker activity stays distinguishable in logs.
    """

    def __init__(
        self,
        name: str,
        process_once: Callable[[], Awaitable[int]],
        idle_secs: int = IDLE_INTERVAL_SECS,
        concurrency: int = 1,
    ):
        self._name = name
        self._process_once = process_once
        self._idle_secs = idle_secs
        self._concurrency = max(1, concurrency)
        self._tasks: list[asyncio.Task] = []
        self._stop_event: Optional[asyncio.Event] = None
        self._log = logging.getLogger(f"akb.{name}")

    def start(self) -> None:
        if self._tasks and any(not t.done() for t in self._tasks):
            return
        self._stop_event = asyncio.Event()
        for i in range(self._concurrency):
            task_name = self._name if self._concurrency == 1 else f"{self._name}-{i}"
            self._tasks.append(asyncio.create_task(self._loop(task_name), name=task_name))

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._tasks:
            # Per-iteration work (embedding API call + vector-store upsert)
            # can legitimately take up to ~60s end-to-end. A short stop
            # timeout cancelled tasks mid-upsert, ROLLBACK-ing the claim's
            # vector_next_attempt_at update and leaving rows in a half-
            # claimed state. Give the current iteration time to finish
            # cleanly; the stop_event still prevents another tick after
            # this one drains.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                self._log.warning(
                    "%s did not stop within 120s; cancelling", self._name,
                )
                for t in self._tasks:
                    if not t.done():
                        t.cancel()
        self._tasks = []
        self._stop_event = None

    async def _loop(self, task_name: str) -> None:
        assert self._stop_event is not None
        log = logging.getLogger(f"akb.{task_name}")
        log.info("%s loop started (idle=%ds, max_retries=%d)",
                 task_name, self._idle_secs, MAX_RETRIES)
        while not self._stop_event.is_set():
            try:
                # Shield the iteration body so a cancellation arriving mid-
                # upsert (shutdown signal) doesn't interrupt the per-chunk
                # transaction. The loop still exits at the top of the next
                # iteration via _stop_event.is_set(); shielding only
                # guarantees the in-flight chunk reaches COMMIT/ROLLBACK
                # cleanly before we tear down.
                done = await asyncio.shield(self._process_once())
            except asyncio.CancelledError:
                # Cancellation reached us despite the shield (e.g., the
                # outer wait_for timed out and cancelled the task). Exit
                # the loop without swallowing — the runner is shutting down.
                raise
            except Exception as e:  # noqa: BLE001 — keep loop alive on any failure
                log.exception("%s iteration failed: %s", task_name, e)
                done = 0

            if done == 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._idle_secs)
                except asyncio.TimeoutError:
                    pass
            else:
                log.info("%s processed %d items", task_name, done)
                await asyncio.sleep(0)

        log.info("%s loop stopped", task_name)
