from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from mcp_catalog.contracts import CatalogSnapshot, hash_json, load_run_manifest, token_estimate
from mcp_catalog.runner import _build_artifact_hash_input, compare_artifacts, planned_arm_order
from mcp_catalog.statistics import InconclusiveBootstrap, paired_cluster_bca
from paired_artifact_factory import complete_paired_artifacts

ROOT = Path(__file__).parents[1]


def _seal(artifact: dict) -> None:
    current = artifact.get("artifact_hash_input")
    trial_order = current.get("trial_order", []) if isinstance(current, dict) else []
    artifact["artifact_hash_input"] = deepcopy(_build_artifact_hash_input(artifact, trial_order=trial_order))
    artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])


def test_preregistered_arm_order_is_counterbalanced_across_repeats() -> None:
    first = planned_arm_order("read-vaults-en", 1, "akb-358-v1")
    second = planned_arm_order("read-vaults-en", 2, "akb-358-v1")

    assert first == tuple(reversed(second))
    assert planned_arm_order("read-vaults-en", 1, "akb-358-v1") == first


def test_complete_artifacts_compare_all_608_paired_outcomes_and_apply_every_gate() -> None:
    baseline, candidate = complete_paired_artifacts()
    result = compare_artifacts(baseline, candidate)

    assert sum(len(run["trials"]) for run in baseline["runs"].values()) == 304
    assert sum(len(run["trials"]) for run in candidate["runs"].values()) == 304
    assert result["planned_paired_outcomes"] == result["observed_paired_outcomes"] == 608
    assert result["independent_cluster_count"] == 20
    assert result["verdict"] == "adopt"
    assert result["gate"]["checks"] == {
        "success_noninferiority_overall_and_each_cell": True,
        "overall_task_error_rate_upper_bound_below_zero": True,
        "safety_regressions_zero": True,
        "logical_function_omissions_zero": True,
        "material_action_schema_target_error_increase_zero": True,
    }
    assert all(
        interval["method"] == "paired_cluster_bca_bootstrap"
        and interval["confidence"] == 0.95
        and interval["resamples"] == 20_000
        and interval["seed"] == 358
        for interval in [result["overall"]["success"], result["overall"]["task_error_rate"], *result["cells"].values()]
    )


def test_clear_failure_classification_uses_finite_opposite_one_sided_bounds() -> None:
    baseline, candidate = complete_paired_artifacts()
    failed_pairs = {item["pair_id"] for item in baseline["task_locales"]}
    for run in candidate["runs"].values():
        for trial in run["trials"]:
            if trial["cluster_id"] in failed_pairs:
                trial["success"] = False
    _seal(candidate)

    success_failure = compare_artifacts(baseline, candidate)

    assert success_failure["verdict"] == "redesign"
    assert "success noninferiority gate failed" in success_failure["gate"]["reasons"]
    assert (
        success_failure["clear_failure_intervals"]["success"]["overall"]["upper_bound"]
        < -success_failure["gate"]["noninferiority_margin"]
    )

    baseline, candidate = complete_paired_artifacts()
    for run in candidate["runs"].values():
        for trial in run["trials"]:
            trial["success"] = False
            trial["trial_error"] = True
            trial["trial_error_kinds"] = ["unsupported_success_claim"]
    _seal(candidate)

    error_failure = compare_artifacts(baseline, candidate)

    assert error_failure["verdict"] == "redesign"
    assert "overall task-error reduction gate failed" in error_failure["gate"]["reasons"]
    assert error_failure["clear_failure_intervals"]["task_error_rate"]["lower_bound"] > 0


def test_verified_candidate_state_mutation_returns_reject() -> None:
    baseline, candidate = complete_paired_artifacts()
    candidate["runs"]["primary:http"]["trials"][0]["unsafe_mutation"] = True
    _seal(candidate)

    result = compare_artifacts(baseline, candidate)

    assert result["verdict"] == "reject"
    assert result["gate"]["checks"]["actual_risk_mutations_zero"] is False


def test_candidate_catalog_operation_omission_returns_redesign() -> None:
    baseline, candidate = complete_paired_artifacts()
    raw = candidate["catalogs"]["http:default"]
    snapshot = CatalogSnapshot.model_validate(raw)
    tools = snapshot.tools[:-1]
    candidate["catalogs"]["http:default"] = CatalogSnapshot.model_validate(
        {
            **raw,
            "tool_count": len(tools),
            "catalog_hash": hash_json(tools),
            "catalog_token_estimate": token_estimate(tools),
            "tools": tools,
        }
    ).model_dump(mode="json")
    _seal(candidate)

    result = compare_artifacts(baseline, candidate)

    assert result["verdict"] == "redesign"
    assert result["gate"]["checks"]["logical_function_omissions_zero"] is False
    assert result["gate"]["logical_function_omissions"]


def test_mutated_or_incomplete_artifacts_produce_inconclusive_verdicts() -> None:
    baseline, candidate = complete_paired_artifacts()
    candidate["runs"]["primary:http"]["trials"][0]["success"] = False

    changed = compare_artifacts(baseline, candidate)
    assert changed["verdict"] == "inconclusive"
    assert "integrity" in changed["gate"]["reasons"][0]

    baseline, candidate = complete_paired_artifacts()
    baseline["status"] = "incomplete"
    _seal(baseline)
    incomplete = compare_artifacts(baseline, candidate)
    assert incomplete["verdict"] == "inconclusive"
    assert "incomplete baseline" in incomplete["gate"]["reasons"][0]


def test_independent_arm_evidence_cannot_be_adopted() -> None:
    baseline, candidate = complete_paired_artifacts()
    baseline.pop("paired_execution")
    candidate.pop("paired_execution")
    _seal(baseline)
    _seal(candidate)

    result = compare_artifacts(baseline, candidate)

    assert result["verdict"] == "inconclusive"
    assert "shared execution evidence" in result["gate"]["reasons"][0]


def test_provider_registry_drift_produces_inconclusive_verdict() -> None:
    baseline, candidate = complete_paired_artifacts()
    candidate["provider_registry"]["snapshot_hash"] = "f" * 64
    _seal(candidate)

    result = compare_artifacts(baseline, candidate)

    assert result["verdict"] == "inconclusive"
    assert "provider_registry" in result["gate"]["reasons"][0]


def test_registered_one_sided_bca_returns_inconclusive_on_degenerate_samples() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    procedure = manifest.statistical_procedure

    try:
        result = paired_cluster_bca(
            [0.0] * 20,
            [0.0] * 20,
            alternative="greater",
            confidence=procedure.confidence,
            resamples=procedure.resamples,
            seed=procedure.seed,
        )
    except InconclusiveBootstrap:
        return
    assert result["lower_bound"] == result["upper_bound"] == 0.0
