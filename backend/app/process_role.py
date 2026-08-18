"""Process composition selector for all-in-one, API, and worker entrypoints."""

from __future__ import annotations

import os
from typing import Literal, cast

ProcessRole = Literal["all", "api", "worker"]


def runtime_process_role() -> ProcessRole:
    value = os.getenv("AKB_PROCESS_ROLE", "all").strip().lower()
    if value not in {"all", "api", "worker"}:
        raise RuntimeError(
            "AKB_PROCESS_ROLE must be one of all, api, worker; "
            f"got {value!r}"
        )
    return cast(ProcessRole, value)
