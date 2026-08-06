"""Low-volume, credential-free events for the repository-owned E2E gate."""

from __future__ import annotations

import json
import sys
import time
from typing import TextIO


EVENT_PREFIX = "AKB_E2E_GATE "


def signal_from_returncode(returncode: int) -> int | None:
    """Return the POSIX signal represented by a negative child return code."""

    return -returncode if returncode < 0 else None


def signal_name(signum: int) -> str:
    """Return a stable signal label without making event emission fail."""

    try:
        import signal

        return signal.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"


def shell_exit_code(returncode: int) -> int:
    """Map a Python subprocess return code to the shell-visible exit code."""

    signum = signal_from_returncode(returncode)
    return 128 + signum if signum is not None else returncode


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
