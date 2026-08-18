"""Kubernetes exec probe for the dedicated worker event loop heartbeat."""

from __future__ import annotations

import os
import time
from pathlib import Path


def main() -> None:
    path = Path(
        os.getenv("AKB_WORKER_HEARTBEAT_PATH", "/run/akb/worker-heartbeat")
    )
    max_age = float(os.getenv("AKB_WORKER_HEARTBEAT_MAX_AGE_SECS", "30"))
    try:
        age = time.time() - path.stat().st_mtime
    except OSError as exc:
        raise SystemExit(f"worker heartbeat unavailable: {exc}") from exc
    if age < 0 or age > max_age:
        raise SystemExit(f"worker heartbeat stale: {age:.1f}s (max {max_age:.1f}s)")


if __name__ == "__main__":
    main()
