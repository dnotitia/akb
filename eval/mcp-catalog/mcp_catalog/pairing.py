"""Deterministic arm scheduling shared by execution and evidence validation."""

from __future__ import annotations

import hashlib

from .contracts import ArmName


def planned_arm_order(task_id: str, repeat_index: int, seed: str) -> tuple[ArmName, ArmName]:
    """Return the preregistered counterbalanced order for one task repeat."""

    digest = hashlib.sha256(f"{seed}:{task_id}".encode()).digest()
    baseline_first = (digest[0] + repeat_index) % 2 == 0
    return ("baseline", "candidate") if baseline_first else ("candidate", "baseline")
