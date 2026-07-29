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
import time
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
        log_progress: bool = True,
    ):
        self._name = name
        self._process_once = process_once
        self._idle_secs = idle_secs
        self._concurrency = max(1, concurrency)
        # Per-tick "processed N items" is signal for a worker whose queue is
        # usually empty (embed, delete, backfill) and noise for one that has
        # work whenever the service has traffic — at a few-second poll that is
        # thousands of INFO lines a day, burying the warnings that matter.
        self._log_progress = log_progress
        self._tasks: list[asyncio.Task] = []
        # In-flight shielded iterations. `_loop` shields `process_once` so a
        # commit is never torn in half, but that also means cancelling the loop
        # task leaves the shielded coroutine running detached. `stop()` awaits
        # these so its timeout bounds when work actually ends, not merely when
        # we stop watching — callers that drain a shared queue afterwards
        # depend on the worker really being finished.
        self._inflight: set[asyncio.Task] = set()
        self._stop_event: Optional[asyncio.Event] = None
        self._log = logging.getLogger(f"akb.{name}")

    def abandoned(self) -> int:
        """Iterations that ignored cancellation and outlived a `stop()`.

        Non-zero means this worker is DEAD for the rest of the process: it
        refuses to start a second loop over a queue the old one may still be
        writing to. Exposed so that shows up on a dashboard rather than in one
        ERROR line at shutdown.
        """
        return len([t for t in self._inflight if not t.done()])

    def start(self) -> None:
        if self._tasks and any(not t.done() for t in self._tasks):
            return
        # A previous `stop()` may have abandoned an iteration that ignored
        # cancellation. Starting a second loop over the same queue while that
        # one is still writing is worse than not starting at all.
        alive = [t for t in self._inflight if not t.done()]
        if alive:
            self._log.error(
                "%s not restarted: %d iteration(s) from the previous run are "
                "still alive", self._name, len(alive),
            )
            return
        self._stop_event = asyncio.Event()
        for i in range(self._concurrency):
            task_name = self._name if self._concurrency == 1 else f"{self._name}-{i}"
            self._tasks.append(asyncio.create_task(self._loop(task_name), name=task_name))

    async def stop(self, timeout: float = 120.0) -> None:
        """Signal the loop and join its work within an ABSOLUTE deadline.

        `timeout` bounds the whole call, and it bounds it even against a
        callback that swallows or delays `CancelledError`: `asyncio.wait`
        returns when the deadline passes without waiting for the cancellation
        to be acknowledged, where `wait_for` would block indefinitely on
        exactly that acknowledgement.

        Anything still running when the budget is gone is logged and LEFT in
        `_inflight` rather than forgotten, so `start()` can refuse to run a
        second loop over the same queue.
        """
        if self._stop_event:
            self._stop_event.set()
        deadline = time.monotonic() + max(0.0, timeout)

        await self._join(self._tasks, deadline, "loop task")
        # The in-flight join runs on EVERY path. `asyncio.shield` detaches the
        # iteration from its wrapper deliberately — so a commit finishes rather
        # than tearing — which also means a wrapper cancelled from anywhere
        # leaves the real work running.
        await self._join([t for t in self._inflight if not t.done()],
                         deadline, "in-flight iteration")

        self._tasks = [t for t in self._tasks if not t.done()]
        self._inflight = {t for t in self._inflight if not t.done()}
        self._stop_event = None

    async def _join(self, tasks: list, deadline: float, what: str) -> None:
        """Wait, then cancel, then give up — never past `deadline`."""
        pending = [t for t in tasks if not t.done()]
        if not pending:
            return
        _, pending_set = await asyncio.wait(pending, timeout=self._remaining(deadline))
        if not pending_set:
            return
        self._log.warning(
            "%s: %d %s(s) did not finish in the stop budget; cancelling",
            self._name, len(pending_set), what,
        )
        for t in pending_set:
            t.cancel()
        _, still = await asyncio.wait(pending_set, timeout=self._remaining(deadline))
        if still:
            self._log.error(
                "%s: %d %s(s) ignored cancellation and are abandoned; the worker "
                "will not restart until they finish", self._name, len(still), what,
            )

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

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
                inner: asyncio.Task = asyncio.ensure_future(self._process_once())
                self._inflight.add(inner)
                try:
                    done = await asyncio.shield(inner)
                finally:
                    # Only forget it once it has actually finished; a cancelled
                    # `shield` leaves `inner` alive and `stop()` must still
                    # await it.
                    if inner.done():
                        self._inflight.discard(inner)
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
                if self._log_progress:
                    log.info("%s processed %d items", task_name, done)
                await asyncio.sleep(0)

        log.info("%s loop stopped", task_name)
