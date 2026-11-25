"""Low-volume, credential-free events for the repository-owned E2E gate."""

from __future__ import annotations

import json
import sys
import time
from typing import TextIO


EVENT_PREFIX = "AKB_E2E_GATE "


def _event_line(event: dict[str, object]) -> bytes:
    payload = {"timestamp": round(time.time(), 3), **event}
    return (
        EVENT_PREFIX
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _write_stderr(line: bytes, stream: TextIO | None = None) -> None:
    try:
        target = stream or sys.stderr
        buffer = getattr(target, "buffer", None)
        if buffer is not None:
            buffer.write(line)
            buffer.flush()
            return
        target.write(line.decode("utf-8", errors="replace"))
        target.flush()
    except (AttributeError, OSError):
        # A terminating parent can close the pipe while a child is emitting
        # its last event.  Event emission is best effort in that case.
        pass


def emit_gate_event(
    event: dict[str, object],
    *,
    stream: TextIO | None = None,
) -> None:
    """Write one safe, machine-readable event to stderr."""

    _write_stderr(_event_line(event), stream)
