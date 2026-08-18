"""Serving-event-loop lag telemetry for /health."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_current_ms = 0.0
_max_ms = 0.0
_over_100ms = 0
_over_1000ms = 0
_last_sample_at: datetime | None = None


async def _run() -> None:
    global _current_ms, _max_ms, _over_100ms, _over_1000ms, _last_sample_at
    assert _stop_event is not None
    interval = 1.0
    expected = time.monotonic() + interval
    while not _stop_event.is_set():
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=interval)
            break
        except TimeoutError:
            pass
        now = time.monotonic()
        lag_ms = max(0.0, (now - expected) * 1000.0)
        _current_ms = lag_ms
        _max_ms = max(_max_ms, lag_ms)
        _over_100ms += int(lag_ms >= 100.0)
        _over_1000ms += int(lag_ms >= 1000.0)
        _last_sample_at = datetime.now(timezone.utc)
        expected = now + interval


def start() -> None:
    global _task, _stop_event
    if _task is not None and not _task.done():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_run(), name="api-loop-monitor")


async def stop() -> None:
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    task, _task = _task, None
    if task is not None:
        await task
    _stop_event = None


def snapshot() -> dict:
    return {
        "current_ms": round(_current_ms, 3),
        "max_ms": round(_max_ms, 3),
        "samples_over_100ms": _over_100ms,
        "samples_over_1000ms": _over_1000ms,
        "last_sample_at": _last_sample_at.isoformat() if _last_sample_at else None,
    }
