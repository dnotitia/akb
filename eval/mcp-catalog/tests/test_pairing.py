from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from mcp_catalog.contracts import hash_json, load_run_manifest
from mcp_catalog.execution import TrialOutcome
from mcp_catalog.runner import _build_artifact_hash_input, compare_artifacts

ROOT = Path(__file__).parents[1]


def _trials(*, candidate: bool) -> list[dict]:
    result: list[dict] = []
    task_ids = ("task-a", "task-b")
    for task_id in task_ids:
        for repeat_index in range(1, 4):
            outcome = TrialOutcome(
                task_id=task_id,
                category="single_operation",
                arm="candidate" if candidate else "baseline",
                model_class="primary",
                model_id="model",
                transport="http",
                repeat_index=repeat_index,
                first_logical_operation="read",
                first_action_accuracy=True,
                argument_validity=True,
                success=True,
                safety=True,
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
                "trials": _trials(candidate=candidate),
            }
        },
    }
    artifact["artifact_hash_input"] = deepcopy(_build_artifact_hash_input(artifact, trial_order=[]))
    artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])
    return artifact


def test_paired_comparison_averages_repeats_per_task_and_applies_all_gates() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    result = compare_artifacts(_artifact(manifest, candidate=False), _artifact(manifest, candidate=True))

    assert result["independent_task_count"] == 2
    assert result["repeat_count"] == 3
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
