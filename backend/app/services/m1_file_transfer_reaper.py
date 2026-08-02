"""Periodic cleanup for guarded M1 File transfer capabilities."""

from __future__ import annotations

from app.services._backfill import BackfillRunner
from app.services.m1_file_measurement import MeasurementFileService, measurement_enabled


REAP_INTERVAL_SECONDS = 60


async def _reap_once() -> int:
    return await MeasurementFileService().reap_transfer_intents()


_runner = BackfillRunner(
    "m1_file_transfer_reaper",
    _reap_once,
    idle_secs=REAP_INTERVAL_SECONDS,
    log_progress=False,
)


def enabled() -> bool:
    return measurement_enabled()


def start() -> None:
    _runner.start()


async def stop(*, timeout: float = 10.0) -> bool:
    return await _runner.stop(timeout=timeout)
