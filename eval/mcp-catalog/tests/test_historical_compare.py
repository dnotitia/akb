from __future__ import annotations

import json
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mcp_catalog import cli
from mcp_catalog.contracts import ModelSpec, TaskManifest, hash_json
from mcp_catalog.execution import registered_route_target
from mcp_catalog.runner import _build_artifact_hash_input
from paired_artifact_factory import complete_paired_artifacts


def _legacy_checkpoint_receipt(checkpoint: dict[str, Any]) -> dict[str, Any]:
    receipt = {
        key: checkpoint.get(key)
        for key in (
            "enabled",
            "lifecycle",
            "record_count",
            "valid_completed_trials",
            "reserved_cost_usd",
            "smoke_status",
            "smoke_cell_count",
        )
    }
    for key in ("attempt_history", "selection_route_revalidations"):
        if key in checkpoint:
            receipt[key] = checkpoint[key]
    return receipt


def _historical_seal(artifact: dict[str, Any]) -> None:
    current = artifact.get("artifact_hash_input")
    trial_order = current.get("trial_order", []) if isinstance(current, dict) else []
    hash_input = _build_artifact_hash_input(artifact, trial_order=trial_order)
    hash_input["checkpoint_receipt"] = _legacy_checkpoint_receipt(artifact["checkpoint"])
    for key in ("continuation_lineage", "continuation_runtime_provenance", "selection", "routing_verification"):
        if key in artifact:
            hash_input[key] = artifact[key]
    artifact["artifact_hash_input"] = hash_input
    artifact["artifact_hash"] = hash_json(hash_input)


def _historical_pair(
    *,
    with_selection: bool = True,
    include_invalid_condition: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline, candidate = complete_paired_artifacts()
    artifacts = {"baseline": baseline, "candidate": candidate}
    source_revisions = {"baseline": "a" * 40, "candidate": "b" * 40}
    manifest = deepcopy(baseline["manifest"])
    manifest["source_revision"] = "runtime_descriptor"
    manifest["arm_source_revisions"] = source_revisions
    manifest["candidate_route_overrides"] = {
        item["operation"]: item["candidate"]
        for item in manifest["public_operations"]
        if item["candidate"] != {"tool": item["operation"], "action": None}
    }
    manifest_hash = hash_json(manifest)

    selected_by_arm: dict[str, list[dict[str, Any]]] = {}
    selected_identities: dict[str, set[tuple[str, str, str, int]]] = {}
    selection_sha256 = "c" * 64 if with_selection else None
    parent_shas = {
        "baseline_artifact": "1" * 64,
        "baseline_checkpoint": "2" * 64,
        "candidate_artifact": "3" * 64,
        "candidate_checkpoint": "4" * 64,
    }
    selection_items: list[dict[str, Any]] = []
    for arm, artifact in artifacts.items():
        plan = artifact["artifact_hash_input"]["trial_order"]
        count = (13 if arm == "baseline" else 10) if with_selection else len(plan)
        chosen = plan[:count]
        if with_selection and include_invalid_condition:
            condition_row = next(item for item in plan if item["task_id"] == "history-provenance-en")
            if condition_row not in chosen:
                chosen = [*chosen[:-1], condition_row]
        identities = {
            (item["model_class"], item["transport"], item["task_id"], item["repeat_index"])
            for item in chosen
        }
        selected_by_arm[arm] = chosen
        selected_identities[arm] = identities
        selection_items.extend(
            {
                "arm": arm,
                "model_class": item["model_class"],
                "transport": item["transport"],
                "task_id": item["task_id"],
                "repeat_index": item["repeat_index"],
                "role": "failure",
                "parent_lineage": "synthetic-parent",
                "failure_kind": "provider",
                "parent_record_hash": hash_json(item),
                "parent_record_key": hash_json(item),
            }
            for item in chosen
        )

    selection = {
        "purpose": "provider_failure_remeasurement",
        "authorization": {"uri": "akb://example/approval", "revision": "d" * 40, "content_sha256": "e" * 64},
        "parents": {
            name: {"path": f"parents/{name}.json", "sha256": digest}
            for name, digest in parent_shas.items()
        },
        "selected_trials": selection_items,
        "expected_trials_by_arm": {arm: len(rows) for arm, rows in selected_by_arm.items()},
        "expected_model_trials": len(selection_items),
        "selection_sha256": selection_sha256,
        "routing_policy": {
            "provider_preference_by_model": {
                model["class_name"]: model["routing"]["order"] for model in manifest["models"]
            },
            "allow_fallbacks": True,
            "require_parameters": True,
            "quantizations": ["fp8"],
        },
        "budget": {
            "session_max_usd": "50",
            "prior_conservative_spend_usd": "0",
            "new_execution_limit_usd": "50",
            "per_trial_usd": "0.10",
            "request_timeout_seconds": manifest["budget"]["request_timeout_seconds"],
        },
    }
    resume_parents = {
        arm: {
            "artifact_hash": hash_json({"parent": arm, "kind": "artifact"}),
            "artifact_sha256": parent_shas[f"{arm}_artifact"],
            "checkpoint_sha256": parent_shas[f"{arm}_checkpoint"],
            "checkpoint_spent_hash": hash_json({"parent": arm, "kind": "spent"}),
            "source_revision": source_revisions[arm],
        }
        for arm in artifacts
    }
    if with_selection:
        for parent in resume_parents.values():
            parent["selection_sha256"] = selection_sha256
    resume_source = {
        "schema_version": 1,
        "parents": resume_parents,
        "source_execution": {"event_count": len(selection_items), "terminal_outcome_count": len(selection_items)},
        "source_accounting": {"checkpoint_model_requests": len(selection_items), "residual": "0"},
        "writer": {"implementation": "synthetic"},
    }
    if with_selection:
        resume_source["selection_sha256"] = selection_sha256

    for arm, artifact in artifacts.items():
        source_revision = source_revisions[arm]
        artifact["manifest"] = deepcopy(manifest)
        artifact["run_manifest_hash"] = manifest_hash
        artifact["source_revision"] = source_revision
        artifact["resume_source"] = deepcopy(resume_source)
        if with_selection:
            artifact["selection"] = deepcopy(selection)
        else:
            artifact.pop("selection", None)
        artifact["paired_budget_used"]["request_timeout_seconds"] = manifest["budget"]["request_timeout_seconds"]
        lineages = {}
        for raw_model in manifest["models"]:
            model = ModelSpec.model_validate(raw_model)
            registered = artifact["provider_registry"]["models"][model.model_id]
            route = registered_route_target(model, registered)
            assert route is not None
            lineages[model.model_id] = route.lineage()
        if with_selection:
            artifact["routing_verification"] = {"status": "verified", "models": lineages}
        else:
            artifact.pop("routing_verification", None)
        if with_selection:
            for report in artifact["runs"].values():
                report["trials"] = [
                    row
                    for row in report["trials"]
                    if (row["model_class"], row["transport"], row["task_id"], row["repeat_index"])
                    in selected_identities[arm]
                ]
        artifact["artifact_hash_input"]["trial_order"] = selected_by_arm[arm]
        for catalog in artifact["catalogs"].values():
            catalog["source_revision"] = source_revision
        artifact["pre_smoke_seal_inputs"]["catalogs"] = deepcopy(artifact["catalogs"])
        artifact["pre_smoke_seal_inputs"]["source_revision"] = source_revision
        artifact["pre_smoke_seal_inputs"]["run_manifest_hash"] = manifest_hash
        artifact["pre_smoke_seal_inputs"]["schema_version"] = 1
        artifact["pre_smoke_seal_inputs"]["paired_order_mode"] = manifest["paired_order"]
        artifact["pre_smoke_seal_inputs"]["paired_order_seed"] = manifest["paired_order_seed"]
        if with_selection:
            artifact["pre_smoke_seal_inputs"]["selection"] = deepcopy(selection)
        else:
            artifact["pre_smoke_seal_inputs"].pop("selection", None)
        artifact["checkpoint"] = {
            "enabled": True,
            "lifecycle": "finalized",
            "record_count": len(selected_by_arm[arm]),
            "valid_completed_trials": len(selected_by_arm[arm]),
            "reserved_cost_usd": 0.0,
            "smoke_status": "passed",
            "smoke_cell_count": 4,
            "attempt_history": [],
        }
    selected_sequences = {
        (item["model_class"], item["transport"], item["task_id"], item["repeat_index"], arm)
        for arm, items in selected_by_arm.items()
        for item in items
    }
    shared_events = (
        [
            event
            for event in baseline["paired_execution"]["events"]
            if (
                event["cell"].split(":", 1)[0],
                event["cell"].split(":", 1)[1],
                event["task_id"],
                event["repeat_index"],
                event["arm"],
            ) in selected_sequences
        ]
        if with_selection
        else baseline["paired_execution"]["events"]
    )
    shared_events.sort(key=lambda item: item["sequence"])
    shared_execution = deepcopy(baseline["paired_execution"])
    shared_execution["events"] = shared_events
    for artifact in artifacts.values():
        artifact["paired_execution"] = deepcopy(shared_execution)
    seal_hash = hash_json({arm: hash_json(artifact["pre_smoke_seal_inputs"]) for arm, artifact in artifacts.items()})
    for artifact in artifacts.values():
        artifact["pre_smoke_seal_hash"] = seal_hash
        _historical_seal(artifact)
    return baseline, candidate


def _hide_preregistered_revision_body(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    task_id = "history-provenance-en"
    for artifact in pair:
        task = next(item for item in artifact["task_contracts"] if item["id"] == task_id)
        body = next(
            attempt["arguments"]["content"]
            for attempt in task["expected_material_attempts"]
            if attempt["logical_operation"] in {"create", "update"}
            and isinstance(attempt["arguments"].get("content"), str)
        )
        task["prompt"] = task["prompt"].replace(body, "omitted revision body", 1)
        corpus = [TaskManifest.model_validate(item).model_dump(mode="json") for item in artifact["task_contracts"]]
        artifact["task_corpus_hash"] = hash_json(corpus)
        artifact["pre_smoke_seal_inputs"]["task_corpus_hash"] = artifact["task_corpus_hash"]
        _historical_seal(artifact)
    seal_hash = hash_json({artifact["arm"]: hash_json(artifact["pre_smoke_seal_inputs"]) for artifact in pair})
    for artifact in pair:
        artifact["pre_smoke_seal_hash"] = seal_hash
        _historical_seal(artifact)


def _assert_raw_evidence_preserved(summary: dict[str, Any], artifact: dict[str, Any]) -> None:
    rows = [row for run in artifact["runs"].values() for row in run["trials"]]
    rescored_by_hash = {row["record_sha256"]: row for row in summary["rescored_trials"]}
    for field in ("model_requests", "input_tokens", "output_tokens", "total_tokens"):
        assert summary[field] == sum(row[field] for row in rows)
    assert Decimal(summary["cost_usd"]) == sum((Decimal(str(row["cost_usd"])) for row in rows), Decimal("0"))
    assert set(rescored_by_hash) == {hash_json(row) for row in rows}
    for raw in rows:
        rescored = rescored_by_hash[hash_json(raw)]
        for field in ("model_requests", "input_tokens", "output_tokens", "total_tokens"):
            assert rescored[field] == raw[field]
        assert Decimal(rescored["cost_usd"]) == Decimal(str(raw["cost_usd"]))
        assert rescored["recorded_success"] == raw["success"]
        assert rescored["recorded_trial_error"] == raw["trial_error"]
        assert rescored["recorded_trial_error_kinds"] == raw["trial_error_kinds"]
        assert rescored["recorded_execution_error"] == raw["error"]
    assert summary["observed_trial_error_trials"] == sum(row["observed_trial_error"] for row in summary["rescored_trials"])
    assert summary["rescored_trial_error_trials"] == sum(row["final_trial_error"] for row in summary["rescored_trials"])


def _invoke_compare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pair: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    baseline, candidate = pair
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    output_path = tmp_path / "comparison.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["mcp-catalog-bench", "compare", "--baseline", str(baseline_path), "--candidate", str(candidate_path), "--output", str(output_path)],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    return json.loads(output_path.read_text(encoding="utf-8"))


def _reseal_manifest_change(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    baseline, candidate = pair
    manifest_hash = hash_json(baseline["manifest"])
    for artifact in pair:
        artifact["run_manifest_hash"] = manifest_hash
        artifact["pre_smoke_seal_inputs"]["run_manifest_hash"] = manifest_hash
        _historical_seal(artifact)
    seal_hash = hash_json(
        {artifact["arm"]: hash_json(artifact["pre_smoke_seal_inputs"]) for artifact in pair}
    )
    for artifact in pair:
        artifact["pre_smoke_seal_hash"] = seal_hash
        _historical_seal(artifact)


def test_compare_cli_rescores_selected_historical_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pair = _historical_pair(include_invalid_condition=True)
    _hide_preregistered_revision_body(pair)
    report = _invoke_compare(monkeypatch, tmp_path, pair)

    assert report["verdict"] == "inconclusive"
    assert report["observed_historical_diagnostics"]["classification"] == "historical_diagnostic_only"
    arms = report["observed_historical_diagnostics"]["arms"]
    assert arms["baseline"]["observed_trials"] == 13
    assert arms["candidate"]["observed_trials"] == 10
    assert arms["baseline"]["model_requests"] > 0
    assert arms["candidate"]["model_requests"] > 0
    assert arms["baseline"]["cost_usd"] != "0"
    assert arms["candidate"]["cost_usd"] != "0"
    assert len(arms["baseline"]["rescored_trials"]) == 13
    assert all("recorded_trial_error" in row and "final_success" in row for row in arms["baseline"]["rescored_trials"])
    for arm in ("baseline", "candidate"):
        summary = arms[arm]
        evaluated = [row for row in summary["rescored_trials"] if row["evaluated"]]
        assert summary["excluded_invalid_condition_trials"] > 0
        assert summary["efficiency_metrics"]["trials"] == summary["evaluated_trials"]
        assert summary["final_successes"] == sum(row["final_success"] for row in evaluated)
        assert summary["efficiency_metrics"]["success_rate"] == pytest.approx(
            sum(row["final_success"] for row in evaluated) / len(evaluated)
        )
        _assert_raw_evidence_preserved(summary, pair[0 if arm == "baseline" else 1])
    baseline_rows = arms["baseline"]["rescored_trials"]
    for row in (
        next(row for row in baseline_rows if row["evaluated"] and row["observed_trial_error"]),
        next(row for row in baseline_rows if not row["evaluated"] and row["observed_trial_error"]),
    ):
        assert row["final_trial_error"] is True


def test_compare_cli_rescores_full_historical_pair_without_adoption(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pair = _historical_pair(with_selection=False)
    _hide_preregistered_revision_body(pair)
    report = _invoke_compare(monkeypatch, tmp_path, pair)

    assert report["verdict"] == "inconclusive"
    diagnostics = report["observed_historical_diagnostics"]
    assert diagnostics["selection"] is None
    assert diagnostics["arms"]["baseline"]["planned_trials"] == 304
    assert diagnostics["arms"]["candidate"]["planned_trials"] == 304
    assert diagnostics["arms"]["baseline"]["observed_trials"] == 304
    assert diagnostics["arms"]["candidate"]["observed_trials"] == 304
    assert diagnostics["arms"]["baseline"]["model_requests"] > 0
    assert diagnostics["arms"]["candidate"]["cost_usd"] != "0"
    for arm_index, arm in enumerate(("baseline", "candidate")):
        summary = diagnostics["arms"][arm]
        evaluated = [row for row in summary["rescored_trials"] if row["evaluated"]]
        assert summary["excluded_invalid_condition_trials"] > 0
        assert summary["efficiency_metrics"]["trials"] == summary["evaluated_trials"]
        assert summary["final_successes"] == sum(row["final_success"] for row in evaluated)
        assert summary["efficiency_metrics"]["success_rate"] == pytest.approx(
            sum(row["final_success"] for row in evaluated) / len(evaluated)
        )
        _assert_raw_evidence_preserved(summary, pair[arm_index])


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("schema", "historical manifest schema or source declaration is invalid"),
        ("source", "historical runtime source revisions do not match the sealed manifest"),
        ("route", "historical public operation route differs from its registry"),
    ],
)
def test_compare_cli_rejects_resealed_historical_contract_mutations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
) -> None:
    pair = _historical_pair()
    manifest = pair[0]["manifest"]
    if mutation == "schema":
        manifest["schema_version"] = 3
    elif mutation == "source":
        manifest["arm_source_revisions"]["baseline"] = "f" * 40
    else:
        operation = manifest["public_operations"][0]["operation"]
        manifest["candidate_route_overrides"][operation] = {"tool": "akb_other_route", "action": "read"}
    pair[1]["manifest"] = deepcopy(manifest)
    _reseal_manifest_change(pair)

    report = _invoke_compare(monkeypatch, tmp_path, pair)

    assert report["verdict"] == "inconclusive"
    assert "observed_historical_diagnostics" not in report
    assert expected_reason in report["gate"]["reasons"][0]


def test_compare_cli_rejects_unresealed_historical_trial_mutation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pair = _historical_pair()
    cell = next(key for key, report in pair[0]["runs"].items() if report["trials"])
    row = pair[0]["runs"][cell]["trials"][0]
    row["success"] = not row["success"]

    report = _invoke_compare(monkeypatch, tmp_path, pair)

    assert report["verdict"] == "inconclusive"
    assert "observed_historical_diagnostics" not in report
    assert "artifact integrity digest does not match" in report["gate"]["reasons"][0]


def test_compare_cli_rejects_malformed_historical_pre_smoke_seal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pair = _historical_pair()
    pair[0]["pre_smoke_seal_inputs"] = None
    _historical_seal(pair[0])

    report = _invoke_compare(monkeypatch, tmp_path, pair)

    assert report["verdict"] == "inconclusive"
    assert "observed_historical_diagnostics" not in report
    assert "pre-smoke seal omits the selection" in report["gate"]["reasons"][0]
