from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from mcp_catalog.contracts import hash_json, load_run_manifest
from mcp_catalog.execution import TrialOutcome
from mcp_catalog.runner import _build_artifact_hash_input, compare_artifacts, planned_arm_order

ROOT = Path(__file__).parents[1]


def _execution_evidence(manifest: dict) -> dict:
    events: list[dict] = []
    sequence = 0
    for repeat_index in range(1, 3):
        for task_id in ("task-a", "task-b"):
            for order_position, arm in enumerate(
                planned_arm_order(task_id, repeat_index, manifest["paired_order_seed"])
            ):
                sequence += 1
                events.append(
                    {
                        "sequence": sequence,
                        "cell": "primary:http",
                        "task_id": task_id,
                        "repeat_index": repeat_index,
                        "arm": arm,
                        "order_position": order_position,
                    }
                )
    return {
        "mode": "counterbalanced_task_repeat",
        "seed": manifest["paired_order_seed"],
        "complete": True,
        "events": events,
        "reused": [],
    }


def _trials(manifest: dict, *, candidate: bool) -> list[dict]:
    result: list[dict] = []
    task_ids = ("task-a", "task-b")
    for task_id in task_ids:
        for repeat_index in range(1, 3):
            outcome = TrialOutcome(
                task_id=task_id,
                category="single_operation",
                arm="candidate" if candidate else "baseline",
                model_class="primary",
                model_id="model",
                transport="http",
                repeat_index=repeat_index,
                paired_order_position=planned_arm_order(
                    task_id,
                    repeat_index,
                    manifest["paired_order_seed"],
                ).index("candidate" if candidate else "baseline"),
                paired_execution_sequence=next(
                    item["sequence"]
                    for item in _execution_evidence(manifest)["events"]
                    if item["task_id"] == task_id
                    and item["repeat_index"] == repeat_index
                    and item["arm"] == ("candidate" if candidate else "baseline")
                ),
                first_logical_operation="read",
                first_action_accuracy=True,
                argument_validity=True,
                success=True,
                safety=True,
                user_outcome_completed=True,
                target_payload_accuracy=True,
                input_tokens=80 if candidate else 90,
                output_tokens=10,
                total_tokens=90 if candidate else 100,
                latency_seconds=0.9 if candidate else 1.0,
            )
            result.append(outcome.model_dump(mode="json"))
    return result


def _artifact(manifest: dict, *, candidate: bool) -> dict:
    arm = "candidate" if candidate else "baseline"
    tokens = 50 if candidate else 100
    artifact = {
        "schema_version": 1,
        "status": "complete",
        "arm": arm,
        "run_manifest_hash": hash_json(manifest),
        "task_corpus_hash": "corpus-hash",
        "task_ids": ["task-a", "task-b"],
        "task_locales": [
            {"id": "task-a", "locale": "en-US", "pair_id": "pair"},
            {"id": "task-b", "locale": "en-US", "pair_id": "pair"},
        ],
        "category_counts": {"single_operation": 2},
        "locale_counts": {"en-US": 2},
        "source_revision": "b" * 40 if candidate else "a" * 40,
        "protocol_revision": "2026-07-28",
        "request_timeout_seconds": manifest["budget"]["request_timeout_seconds"],
        "artifact_versions": {},
        "fixture": {"scenario": "app-control-plane", "reset": {"method": "POST", "url": "http://fixture/reset", "body": {"scenario": "app-control-plane"}}},
        "manifest": manifest,
        "smoke_gate": {"status": "passed", "required_cells": [], "cells": []},
        "overall_metrics": {},
        "locale_metrics": {},
        "catalogs": {
            "http:default": {
                "catalog_token_estimate": tokens,
            }
        },
        "runs": {
            "primary:http": {
                "trials": _trials(manifest, candidate=candidate),
            }
        },
        "paired_execution": _execution_evidence(manifest),
        "paired_budget_used": {
            "model_requests": 8,
            "input_tokens": 720,
            "output_tokens": 80,
            "total_tokens": 800,
            "cost_usd": 0.01,
            "wall_seconds": 1.0,
            "model_work_seconds": 1.0,
            "max_total_cost_usd": 50.0,
        },
    }
    artifact["artifact_hash_input"] = deepcopy(_build_artifact_hash_input(artifact, trial_order=[]))
    artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])
    return artifact


def _seal(artifact: dict) -> None:
    artifact["artifact_hash_input"] = deepcopy(_build_artifact_hash_input(artifact, trial_order=[]))
    artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])


def test_paired_comparison_averages_repeats_per_task_and_applies_all_gates() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    result = compare_artifacts(_artifact(manifest, candidate=False), _artifact(manifest, candidate=True))

    assert result["independent_task_count"] == 2
    assert result["repeat_count"] == 2
    assert result["paired"]["primary:http"]["metrics"]["success"]["independent_tasks"] == 2
    assert result["gate"]["status"] == "pass"
    assert result["gate"]["catalog_reduction"]["http:default"] == 0.5


def test_comparison_rejects_run_results_changed_after_artifact_hashing() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    baseline = _artifact(manifest, candidate=False)
    candidate = _artifact(manifest, candidate=True)
    candidate["runs"]["primary:http"]["trials"][0]["success"] = False

    with pytest.raises(ValueError, match="artifact hash"):
        compare_artifacts(baseline, candidate)


def test_incomplete_arm_artifact_cannot_be_paired() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    baseline = _artifact(manifest, candidate=False)
    candidate = _artifact(manifest, candidate=True)
    baseline["status"] = "incomplete"

    with pytest.raises(ValueError, match="incomplete baseline"):
        compare_artifacts(baseline, candidate)


def test_comparison_rejects_independent_arm_runs_without_execution_order_evidence() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    baseline = _artifact(manifest, candidate=False)
    candidate = _artifact(manifest, candidate=True)
    baseline.pop("paired_execution")
    candidate.pop("paired_execution")
    _seal(baseline)
    _seal(candidate)

    with pytest.raises(ValueError, match="shared execution evidence"):
        compare_artifacts(baseline, candidate)


def test_preregistered_arm_order_is_counterbalanced_across_repeats() -> None:
    first = planned_arm_order("read-vaults-en", 1, "registered-seed")
    second = planned_arm_order("read-vaults-en", 2, "registered-seed")

    assert first == tuple(reversed(second))
    assert planned_arm_order("read-vaults-en", 1, "registered-seed") == first


def test_provider_distribution_imbalance_downgrades_comparison_to_inconclusive() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    baseline = _artifact(manifest, candidate=False)
    candidate = _artifact(manifest, candidate=True)
    for trial in baseline["runs"]["primary:http"]["trials"]:
        trial["provider_evidence"] = [{"routing": {"provider": "parasail"}}]
    for trial in candidate["runs"]["primary:http"]["trials"]:
        trial["provider_evidence"] = [{"routing": {"provider": "fallback-provider"}}]
    _seal(baseline)
    _seal(candidate)

    result = compare_artifacts(baseline, candidate)

    assert result["gate"]["status"] == "inconclusive"
    assert result["gate"]["checks"]["provider_distribution_balanced"] is False
    assert result["gate"]["provider_sensitivity"]["maximum_share_difference"] == 1.0


def test_discouraged_preflight_is_a_paired_semantic_behavior_metric() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    baseline = _artifact(manifest, candidate=False)
    candidate = _artifact(manifest, candidate=True)
    for trial in baseline["runs"]["primary:http"]["trials"]:
        trial["discouraged_preflight_calls"] = 1
    _seal(baseline)
    _seal(candidate)

    result = compare_artifacts(baseline, candidate)
    metric = result["paired"]["primary:http"]["metrics"]["discouraged_preflight_calls"]

    assert metric["baseline_mean"] == 1.0
    assert metric["candidate_mean"] == 0.0
    assert result["paired"]["primary:http"]["gate"]["semantic_behavior_improvement"] is True
