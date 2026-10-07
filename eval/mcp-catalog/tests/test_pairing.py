from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from mcp_catalog.contracts import CatalogSnapshot, hash_json, load_run_manifest, load_task_corpus, token_estimate
from mcp_catalog.runner import _build_artifact_hash_input, compare_artifacts, planned_arm_order
from mcp_catalog.statistics import InconclusiveBootstrap, paired_cluster_bca
from paired_artifact_factory import complete_paired_artifacts, mark_candidate_unsafe

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
    pairs = list(dict.fromkeys(task.pair_id for task in load_task_corpus(ROOT / "corpus" / "tasks.json")))
    baseline, candidate = complete_paired_artifacts(
        baseline_failure_pairs=set(pairs[:5]),
        candidate_failure_pairs=set(pairs),
    )

    success_failure = compare_artifacts(baseline, candidate)

    assert success_failure["verdict"] == "redesign"
    assert "success noninferiority gate failed" in success_failure["gate"]["reasons"]
    assert (
        success_failure["clear_failure_intervals"]["success"]["overall"]["upper_bound"]
        < -success_failure["gate"]["noninferiority_margin"]
    )

    baseline, candidate = complete_paired_artifacts(
        baseline_failure_pairs=set(pairs[:5]),
        candidate_failure_pairs=set(pairs),
    )

    error_failure = compare_artifacts(baseline, candidate)

    assert error_failure["verdict"] == "redesign"
    assert "overall task-error reduction gate failed" in error_failure["gate"]["reasons"]
    assert error_failure["clear_failure_intervals"]["task_error_rate"]["lower_bound"] > 0


def test_verified_candidate_state_mutation_returns_reject() -> None:
    baseline, candidate = complete_paired_artifacts()
    mark_candidate_unsafe(candidate)
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


def test_missing_state_or_raw_call_evidence_is_rejected_after_resealing() -> None:
    baseline, candidate = complete_paired_artifacts()
    trial = candidate["runs"]["primary:http"]["trials"][0]
    trial["state_observations_after"] = []
    _seal(candidate)

    missing_state = compare_artifacts(baseline, candidate)
    assert missing_state["verdict"] == "inconclusive"
    assert "state" in missing_state["gate"]["reasons"][0]

    baseline, candidate = complete_paired_artifacts()
    trial = candidate["runs"]["primary:http"]["trials"][0]
    trial["tool_calls"] = []
    _seal(candidate)

    missing_calls = compare_artifacts(baseline, candidate)
    assert missing_calls["verdict"] == "inconclusive"
    assert "raw call" in missing_calls["gate"]["reasons"][0]

    baseline, candidate = complete_paired_artifacts()
    trial = candidate["runs"]["primary:http"]["trials"][0]
    trial.pop("tool_calls")
    _seal(candidate)

    missing_call_trace = compare_artifacts(baseline, candidate)
    assert missing_call_trace["verdict"] == "inconclusive"
    assert "raw call" in missing_call_trace["gate"]["reasons"][0]


def test_resealed_success_flag_and_preregistered_seal_tampering_are_rejected() -> None:
    baseline, candidate = complete_paired_artifacts()
    trial = next(
        item
        for item in candidate["runs"]["primary:http"]["trials"]
        if item["task_id"] == "ambiguous-clarification-ko"
    )
    trial["success"] = True
    _seal(candidate)

    changed_score = compare_artifacts(baseline, candidate)
    assert changed_score["verdict"] == "inconclusive"
    assert "success" in changed_score["gate"]["reasons"][0]

    baseline, candidate = complete_paired_artifacts()
    candidate["pre_smoke_seal_inputs"]["oracle_hash"] = "f" * 64
    _seal(candidate)

    changed_oracle = compare_artifacts(baseline, candidate)
    assert changed_oracle["verdict"] == "inconclusive"
    assert "oracle" in changed_oracle["gate"]["reasons"][0]


def test_resealed_shared_budget_mismatch_is_rejected() -> None:
    baseline, candidate = complete_paired_artifacts()
    baseline["paired_budget_used"]["model_requests"] += 1
    candidate["paired_budget_used"]["model_requests"] += 1
    _seal(baseline)
    _seal(candidate)

    result = compare_artifacts(baseline, candidate)

    assert result["verdict"] == "inconclusive"
    assert "budget" in result["gate"]["reasons"][0]


def test_external_compare_cli_writes_all_four_verdicts(tmp_path: Path) -> None:
    pairs = list(dict.fromkeys(task.pair_id for task in load_task_corpus(ROOT / "corpus" / "tasks.json")))
    cases = [
        ("adopt", complete_paired_artifacts()),
        (
            "redesign",
            complete_paired_artifacts(
                baseline_failure_pairs=set(pairs[:5]),
                candidate_failure_pairs=set(pairs),
            ),
        ),
    ]
    baseline, candidate = complete_paired_artifacts()
    mark_candidate_unsafe(candidate)
    _seal(candidate)
    cases.append(("reject", (baseline, candidate)))
    cases.append(("inconclusive", complete_paired_artifacts(mirror_baseline_failures=True)))

    for index, (expected_verdict, (baseline, candidate)) in enumerate(cases):
        baseline_path = tmp_path / f"baseline-{index}.json"
        candidate_path = tmp_path / f"candidate-{index}.json"
        output_path = tmp_path / f"comparison-{index}.json"
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_catalog.cli",
                "compare",
                "--baseline",
                str(baseline_path),
                "--candidate",
                str(candidate_path),
                "--output",
                str(output_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == (0 if expected_verdict == "adopt" else 1), completed.stderr
        assert output_path.is_file()
        report = json.loads(output_path.read_text(encoding="utf-8"))
        assert report["verdict"] == expected_verdict


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
    assert result["lower_bound"] == 0.0
    assert result["upper_bound"] is None


def test_one_sided_unbounded_interval_is_strict_json() -> None:
    greater = paired_cluster_bca([0.0, 0.1, 0.2], [0.2, 0.4, 0.6], alternative="greater")
    less = paired_cluster_bca([0.0, 0.1, 0.2], [0.2, 0.4, 0.6], alternative="less")

    assert greater["upper_bound"] is None
    assert less["lower_bound"] is None
    json.dumps({"greater": greater, "less": less}, allow_nan=False)
