"""Periodic cleanup for guarded M1 File transfer capabilities."""

from __future__ import annotations

from app.services._backfill import BackfillRunner
from app.services.m1_file_measurement import reap_transfer_intents


REAP_INTERVAL_SECONDS = 60


async def _reap_once() -> int:
    return await reap_transfer_intents()


_runner = BackfillRunner(
    "m1_file_transfer_reaper",
    _reap_once,
    idle_secs=REAP_INTERVAL_SECONDS,
    log_progress=False,
)


def enabled() -> bool:
    """Always. Both lanes write capabilities to this table, and the one that
    ships (`s3_current` download capabilities) has no other reaper. Binding
    this to measurement mode is what left expiry enforced by a single `WHERE`
    predicate on the read path. An empty table costs one indexed scan a minute
    (`idx_m1_file_transfer_expiry`, migration 055)."""
    return True


def start() -> None:
    _runner.start()


async def stop(*, timeout: float = 10.0) -> bool:
    return await _runner.stop(timeout=timeout)
