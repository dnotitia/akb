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
    """Owns the asyncio task lifecycle for one backfill worker.

    The caller supplies `process_once`, an async callable returning the
    number of items processed. We handle the idle/drain cadence and
    graceful stop.
    """

    def __init__(self, name: str, process_once: Callable[[], Awaitable[int]]):
        self._name = name
        self._process_once = process_once
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._log = logging.getLogger(f"akb.{name}")

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name=self._name)

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None
        self._stop_event = None

    async def _loop(self) -> None:
        assert self._stop_event is not None
        self._log.info("%s loop started (idle=%ds, max_retries=%d)",
                       self._name, IDLE_INTERVAL_SECS, MAX_RETRIES)
        while not self._stop_event.is_set():
            try:
                done = await self._process_once()
            except Exception as e:  # noqa: BLE001 — keep loop alive on any failure
                self._log.exception("%s iteration failed: %s", self._name, e)
                done = 0

            if done == 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=IDLE_INTERVAL_SECS)
                except asyncio.TimeoutError:
                    pass
            else:
                self._log.info("%s processed %d items", self._name, done)
                await asyncio.sleep(0)

        self._log.info("%s loop stopped", self._name)
