from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from mcp_catalog.contracts import hash_json
from mcp_catalog.execution import TrialOutcome
from mcp_catalog.runner import _reconcile_paired_artifact_reservations


ORPHANED = "benchmark incomplete: artifact has an orphaned reservation"
IDENTITY = {
    "model_class": "primary",
    "transport": "http",
    "task_id": "read-vaults-en",
    "repeat_index": 1,
}
SMOKE_CELLS = ["primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio"]


def _outcome(
    arm: str,
    *,
    task_id: str,
    model_class: str,
    transport: str,
    repeat_index: int = 1,
    cost: float = 0.001,
    latency: float = 0.5,
) -> dict[str, Any]:
    return TrialOutcome(
        task_id=task_id,
        category="single_operation",
        arm=arm,
        model_class=model_class,
        model_id=f"{model_class}-fixture",
        transport=transport,
        repeat_index=repeat_index,
        model_requests=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cost_usd=cost,
        provider_cost_usd=cost,
        latency_seconds=latency,
    ).model_dump(mode="json")


def _arm_artifact(arm: str, *, stale_reservation: bool = False) -> dict[str, Any]:
    trial = _outcome(
        arm,
        task_id="read-vaults-en",
        model_class="primary",
        transport="http",
        cost=0.01,
        latency=2.0,
    )
    smoke_cells = []
    outcomes = [trial]
    for cell in SMOKE_CELLS:
        model_class, transport = cell.split(":", 1)
        outcome = _outcome(
            arm,
            task_id=f"smoke-{model_class}-{transport}",
            model_class=model_class,
            transport=transport,
        )
        outcomes.append(outcome)
        smoke_cells.append({"cell": cell, "status": "completed", "outcome": outcome})

    spent = {
        "model_requests": sum(row["model_requests"] for row in outcomes),
        "provider_setup_requests": 0,
        "input_tokens": sum(row["input_tokens"] for row in outcomes),
        "output_tokens": sum(row["output_tokens"] for row in outcomes),
        "cost_usd": sum(row["cost_usd"] for row in outcomes),
        "wall_seconds": 4.0,
        "model_work_seconds": sum(row["latency_seconds"] for row in outcomes),
    }
    trial_order = [deepcopy(IDENTITY)]
    checkpoint = {
        "enabled": True,
        "lifecycle": "finalized",
        "reserved_cost_usd": 0.0,
        "record_count": 1,
        "valid_completed_trials": 1,
        "smoke_status": "passed",
        "smoke_cell_count": 4,
        "spent": spent,
        "spent_hash": hash_json(spent),
    }
    return {
        "arm": arm,
        "status": "incomplete" if stale_reservation else "complete",
        "incomplete_reasons": [ORPHANED] if stale_reservation else [],
        "completed_trials": 1,
        "artifact_hash_input": {
            "trial_order": trial_order,
            "checkpoint_receipt": {
                key: checkpoint[key]
                for key in (
                    "enabled",
                    "lifecycle",
                    "record_count",
                    "valid_completed_trials",
                    "reserved_cost_usd",
                    "smoke_status",
                    "smoke_cell_count",
                )
            },
        },
        "checkpoint": checkpoint,
        "budget_used": {
            **spent,
            "total_tokens": spent["input_tokens"] + spent["output_tokens"],
            "reserved_cost_usd": 0.0,
        },
        "smoke_gate": {
            "status": "passed",
            "required_cells": list(SMOKE_CELLS),
            "cells": smoke_cells,
        },
        "runs": {
            "primary:http": {
                "trials": [trial],
                "statuses": [
                    {"task_id": "read-vaults-en", "repeat_index": 1, "status": "completed"}
                ],
            }
        },
    }


def _paired_budget(
    artifacts: dict[str, dict[str, Any]],
    *,
    final_reserved: str = "0",
    mismatch_requests: int = 0,
) -> dict[str, Any]:
    outcomes = [
        row
        for artifact in artifacts.values()
        for cell in artifact["smoke_gate"]["cells"]
        for row in [cell["outcome"]]
    ]
    outcomes.extend(
        row
        for artifact in artifacts.values()
        for run in artifact["runs"].values()
        for row in run["trials"]
    )
    input_tokens = sum(row["input_tokens"] for row in outcomes)
    output_tokens = sum(row["output_tokens"] for row in outcomes)
    return {
        "model_requests": sum(row["model_requests"] for row in outcomes) + mismatch_requests,
        "provider_setup_requests": 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": sum(max(row["cost_usd"], row["provider_cost_usd"] or 0.0) for row in outcomes),
        "wall_seconds": 5.0,
        "model_work_seconds": sum(row["latency_seconds"] for row in outcomes),
        "max_total_cost_usd": 50.0,
        "final_reserved_cost_usd": final_reserved,
    }


def test_terminal_pair_clears_only_the_transient_peer_reservation_reason() -> None:
    artifacts = {
        "baseline": _arm_artifact("baseline"),
        "candidate": _arm_artifact("candidate", stale_reservation=True),
    }

    _reconcile_paired_artifact_reservations(
        artifacts,
        paired_budget_used=_paired_budget(artifacts),
        paired_run_terminal=True,
    )

    assert artifacts["baseline"]["status"] == "complete"
    assert artifacts["candidate"]["status"] == "complete"
    assert artifacts["candidate"]["incomplete_reasons"] == []


def test_resume_source_residual_is_added_to_checkpoint_totals_once() -> None:
    artifacts = {
        "baseline": _arm_artifact("baseline"),
        "candidate": _arm_artifact("candidate", stale_reservation=True),
    }
    source_residual = {
        "model_requests": 13,
        "provider_setup_requests": 0,
        "input_tokens": 7,
        "output_tokens": 3,
        "conservative_cost_usd": "0.0015",
        "model_work_seconds": 1.25,
    }
    source = {"source_accounting": {"residual": source_residual}}
    for artifact in artifacts.values():
        artifact["resume_source"] = source

    budget = _paired_budget(artifacts)
    for field in ("model_requests", "provider_setup_requests", "input_tokens", "output_tokens"):
        budget[field] += source_residual[field]
    budget["total_tokens"] = budget["input_tokens"] + budget["output_tokens"]
    budget["cost_usd"] += float(source_residual["conservative_cost_usd"])
    budget["model_work_seconds"] += source_residual["model_work_seconds"]

    _reconcile_paired_artifact_reservations(
        artifacts,
        paired_budget_used=budget,
        paired_run_terminal=True,
    )

    assert artifacts["baseline"]["status"] == "complete"
    assert artifacts["candidate"]["status"] == "complete"


@pytest.mark.parametrize(
    "change,final_reserved,mismatch_requests,terminal",
    [
        ("reservation", "0.01", 0, True),
        ("receipt", "0", 1, True),
        ("checkpoint", "0", 0, True),
        ("nonterminal", "0", 0, False),
    ],
)
def test_terminal_reservation_receipt_or_checkpoint_gap_stays_incomplete(
    change: str,
    final_reserved: str,
    mismatch_requests: int,
    terminal: bool,
) -> None:
    artifacts = {
        "baseline": _arm_artifact("baseline"),
        "candidate": _arm_artifact("candidate", stale_reservation=True),
    }
    if change == "checkpoint":
        artifacts["candidate"]["checkpoint"]["lifecycle"] = "open"

    _reconcile_paired_artifact_reservations(
        artifacts,
        paired_budget_used=_paired_budget(
            artifacts,
            final_reserved=final_reserved,
            mismatch_requests=mismatch_requests,
        ),
        paired_run_terminal=terminal,
    )

    assert artifacts["candidate"]["status"] == "incomplete"
    assert ORPHANED in artifacts["candidate"]["incomplete_reasons"]
