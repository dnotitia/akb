from __future__ import annotations

import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_catalog.cli as cli_module
import mcp_catalog.selection as selection_module
from mcp_catalog.checkpoint import CheckpointKey, checkpoint_key_digest, checkpoint_record_hash
from mcp_catalog.contracts import hash_json, load_run_manifest, load_task_corpus
from mcp_catalog.execution import TrialOutcome
from mcp_catalog.pairing import planned_arm_order
from mcp_catalog.runner import BenchmarkRunner, PairedArmCoordinator
from mcp_catalog.runtime import RuntimeDescriptor
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


def _input_manifest():
    return load_run_manifest(ROOT / "config" / "run.json")


def _parent_manifest():
    manifest = _input_manifest()
    models = []
    for model in manifest.models:
        provider = "streamlake" if model.class_name == "primary" else "chutes"
        models.append(
            model.model_copy(
                update={
                    "routing": model.routing.model_copy(update={"order": [provider], "allow_fallbacks": False}),
                    "input_cost_per_million_usd": 0.44 if model.class_name == "primary" else 0.225,
                    "output_cost_per_million_usd": 1.32 if model.class_name == "primary" else 1.98,
                }
            )
        )
    return manifest.model_copy(
        update={
            "models": models,
            "arm_source_revisions": selection_module.SOURCE_REVISIONS,
        }
    )


def _write_parent_files(
    tmp_path: Path,
    tasks: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, str]]:
    manifest = _parent_manifest()
    corpus_hash = hash_json([task.model_dump(mode="json") for task in tasks])
    manifest_hash = hash_json(manifest.model_dump(mode="json"))
    model_ids = {model.class_name: model.model_id for model in manifest.models}
    task_by_id = {task.id: task for task in tasks}
    tmp_path.mkdir(parents=True, exist_ok=True)
    parent_bindings: dict[str, dict[str, str]] = {}

    for arm in ("baseline", "candidate"):
        run_rows: dict[str, list[dict[str, Any]]] = {}
        run_statuses: dict[str, list[dict[str, Any]]] = {}
        records: dict[str, dict[str, Any]] = {}
        for sequence, item in enumerate(
            (failure for failure in selection_module.APPROVED_FAILURES if failure[0] == arm),
            start=1,
        ):
            _, model_class, transport, task_id, repeat_index, failure_kind = item
            task = task_by_id[task_id]
            key = CheckpointKey(
                source_revision=manifest.arm_source_revisions[arm],
                run_manifest_hash=manifest_hash,
                task_corpus_hash=corpus_hash,
                arm=arm,
                model_class=model_class,
                model_id=model_ids[model_class],
                transport=transport,
                task_id=task_id,
                repeat_index=repeat_index,
                locale=task.locale,
            )
            key_digest = checkpoint_key_digest(key)
            order_position = planned_arm_order(task_id, repeat_index, manifest.paired_order_seed).index(arm)
            outcome = TrialOutcome(
                task_id=task_id,
                category=task.category,
                locale=task.locale,
                arm=arm,
                model_class=model_class,
                model_id=model_ids[model_class],
                transport=transport,
                repeat_index=repeat_index,
                paired_order_position=order_position,
                paired_execution_sequence=sequence,
                error=f"source {failure_kind}",
                failure_kind=failure_kind,
            )
            outcome_json = outcome.model_dump(mode="json")
            record_hash = checkpoint_record_hash("failed", key, outcome)
            records[key_digest] = {
                "schema_version": 4,
                "status": "failed",
                "key": key.model_dump(mode="json"),
                "outcome": outcome_json,
                "record_hash": record_hash,
            }
            run_key = f"{model_class}:{transport}"
            run_rows.setdefault(run_key, []).append(outcome_json)
            run_statuses.setdefault(run_key, []).append(
                {"task_id": task_id, "repeat_index": repeat_index, "status": "failed"}
            )

        provider_registry = {"status": "verified", "snapshot_hash": "b" * 64}
        artifact = {
            "schema_version": 2,
            "arm": arm,
            "source_revision": manifest.arm_source_revisions[arm],
            "manifest": manifest.model_dump(mode="json"),
            "task_corpus_hash": corpus_hash,
            "run_manifest_hash": manifest_hash,
            "pre_smoke_seal_hash": "a" * 64,
            "provider_registry": provider_registry,
            "runs": {
                run_key: {"trials": rows, "statuses": run_statuses[run_key]}
                for run_key, rows in run_rows.items()
            },
        }
        checkpoint = {
            "schema_version": 4,
            "lifecycle": "finalized",
            "header": {
                "schema_version": 4,
                "source_revision": manifest.arm_source_revisions[arm],
                "run_manifest_hash": manifest_hash,
                "task_corpus_hash": corpus_hash,
                "arm": arm,
                "pre_smoke_seal_hash": "a" * 64,
                "provider_registry_hash": provider_registry["snapshot_hash"],
            },
            "records": records,
        }
        for file_kind, value in (("artifact", artifact), ("checkpoint", checkpoint)):
            path = tmp_path / f"{arm}.{file_kind}.json"
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            path.write_bytes(raw)
            parent_bindings[f"{arm}_{file_kind}"] = {
                "path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

    monkeypatch.setattr(
        selection_module,
        "APPROVED_PARENT_SHA256",
        {name: binding["sha256"] for name, binding in parent_bindings.items()},
    )
    return parent_bindings


def _write_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[Any], selection_module.ToolGuidanceSelection]:
    manifest = _input_manifest()
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    parents = _write_parent_files(tmp_path, tasks, monkeypatch)
    parent_documents = {
        name: json.loads(Path(binding["path"]).read_text(encoding="utf-8"))
        for name, binding in parents.items()
    }
    selected = []
    for arm, model_class, transport, task_id, repeat_index, failure_kind in selection_module.APPROVED_FAILURES:
        artifact = parent_documents[f"{arm}_artifact"]
        checkpoint = parent_documents[f"{arm}_checkpoint"]
        row = next(
            row
            for run in artifact["runs"].values()
            for row in run["trials"]
            if row["model_class"] == model_class
            and row["transport"] == transport
            and row["task_id"] == task_id
            and row["repeat_index"] == repeat_index
        )
        key = next(
            key
            for key, record in checkpoint["records"].items()
            if record["outcome"] == row
        )
        record = checkpoint["records"][key]
        selected.append(
            {
                "arm": arm,
                "model_class": model_class,
                "transport": transport,
                "task_id": task_id,
                "repeat_index": repeat_index,
                "role": "failure",
                "parent_lineage": "resume04",
                "parent_record_key": key,
                "parent_record_hash": record["record_hash"],
                "failure_kind": failure_kind,
            }
        )
    document = {
        "schema_version": 1,
        "authorization": selection_module.APPROVED_AUTHORIZATION.copy(),
        "parents": parents,
        "selected_trials": selected,
        "budget": {
            "session_max_usd": "50",
            "prior_conservative_spend_usd": "11.1961127352",
            "new_execution_limit_usd": "38.8038872648",
            "per_trial_usd": "0.10",
            "request_timeout_seconds": 300,
        },
    }
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    selection = selection_module.ToolGuidanceSelection.load(path, manifest=manifest, tasks=tasks)
    return manifest, tasks, selection


def test_approved_selection_pins_current_authority_four_parents_and_23_failures() -> None:
    assert selection_module.APPROVED_AUTHORIZATION == {
        "uri": "akb://reef-akb/coll/issues/doc/akb-361.md",
        "revision": "c7ffee0f74e3f85d1ce8502f343c16d51a6bb83d",  # pragma: allowlist secret
        "content_sha256": "b1bed7a8febaad288255a55ad0caaacc7fe55aa1d114d7319a47740f0dda9b7f",  # pragma: allowlist secret
    }
    assert selection_module.APPROVED_PARENT_SHA256 == {
        "baseline_artifact": "aee24ffd43b29636cb1ea50994fef068183b5ce6013a10f30cc29980e87d9a0d",  # pragma: allowlist secret
        "baseline_checkpoint": "87a78c5929685e7586b567648c40ac1d159d40e1928fff611626fda630b9a798",  # pragma: allowlist secret
        "candidate_artifact": "b4f22694020812e2cdd99058fd3153958a5cce491fb830f6011baacb6c214987",  # pragma: allowlist secret
        "candidate_checkpoint": "ded15e8564780025738acd4bbd60a3f3b389f9febfe228c02d7951a48a88d623",  # pragma: allowlist secret
    }
    assert len(selection_module.APPROVED_FAILURES) == 23
    assert Counter(item[0] for item in selection_module.APPROVED_FAILURES) == Counter(baseline=13, candidate=10)
    assert Counter(item[5] for item in selection_module.APPROVED_FAILURES) == Counter(
        provider=12, unknown=5, request_timeout=4, interrupted=2
    )


def test_selection_loads_parent_rows_read_only_and_builds_the_approved_fallback_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _write_selection(tmp_path, monkeypatch)
    parent_bytes = {name: Path(parent["path"]).read_bytes() for name, parent in selection.parents.items()}
    metadata = selection.artifact_metadata()

    assert len(selection.selected_trials) == 23
    assert selection.expected_trials_by_arm == {"baseline": 13, "candidate": 10}
    assert Counter(trial.failure_kind for trial in selection.selected_trials) == Counter(
        provider=12, unknown=5, request_timeout=4, interrupted=2
    )
    assert set(selection.parents) == {
        "baseline_artifact",
        "baseline_checkpoint",
        "candidate_artifact",
        "candidate_checkpoint",
    }
    assert metadata["expected_model_trials"] == 23
    assert metadata["routing_policy"]["allow_fallbacks"] is True
    assert selection.budget.new_execution_limit_usd == Decimal("38.8038872648")
    assert all(model.routing.allow_fallbacks for model in selection.execution_manifest.models)
    assert next(model for model in manifest.models if model.class_name == "primary").routing.allow_fallbacks is False
    assert next(model for model in selection.execution_manifest.models if model.class_name == "primary").routing.order == [
        "streamlake"
    ]
    assert next(model for model in selection.execution_manifest.models if model.class_name == "lightweight").routing.order == [
        "chutes"
    ]
    assert {model.class_name: model.input_cost_per_million_usd for model in selection.execution_manifest.models} == {
        "primary": 0.44,
        "lightweight": 0.225,
    }
    assert {name: Path(parent["path"]).read_bytes() for name, parent in selection.parents.items()} == parent_bytes
    assert selection.checkpoint_preflight()["selected_trial_count"] == 23


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("authorization", "approved AKB-361 revision"),
        ("missing", "23 approved parent failures"),
        ("extra", "23 approved parent failures"),
        ("duplicate", "duplicate arm-specific"),
        ("wrong_arm", "23 approved parent failures"),
        ("wrong_failure_kind", "failure kind differs"),
        ("wrong_parent_record", "parent checkpoint record or record hash"),
    ],
)
def test_selection_rejects_changes_to_approval_identity_or_record_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    manifest, tasks, selection = _write_selection(tmp_path, monkeypatch)
    raw = json.loads(selection.path.read_text(encoding="utf-8"))
    if mutation == "authorization":
        raw["authorization"]["revision"] = "0" * 40
    elif mutation == "missing":
        raw["selected_trials"].pop()
    elif mutation == "extra":
        raw["selected_trials"][0]["task_id"] = "unregistered-task"
    elif mutation == "duplicate":
        raw["selected_trials"][1] = dict(raw["selected_trials"][0])
    elif mutation == "wrong_arm":
        raw["selected_trials"][0]["arm"] = "candidate"
    elif mutation == "wrong_failure_kind":
        raw["selected_trials"][0]["failure_kind"] = "unknown"
    else:
        raw["selected_trials"][0]["parent_record_hash"] = "f" * 64
    selection.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)


def test_selection_rejects_changed_parent_bytes_and_budget_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _write_selection(tmp_path, monkeypatch)
    parent_path = Path(selection.parents["baseline_artifact"]["path"])
    parent_path.write_bytes(parent_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)

    _manifest, _tasks, second = _write_selection(tmp_path / "budget", monkeypatch)
    raw = json.loads(second.path.read_text(encoding="utf-8"))
    raw["budget"]["prior_conservative_spend_usd"] = "10"
    second.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="selection budget differs"):
        selection_module.ToolGuidanceSelection.load(second.path, manifest=manifest, tasks=tasks)


def test_selection_schedules_only_the_authorized_arm_and_never_runs_paid_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, tasks, selection = _write_selection(tmp_path, monkeypatch)
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    coordinator = PairedArmCoordinator(selection.execution_manifest, tasks, selection=selection)
    planned_by_arm = {
        arm: BenchmarkRunner(
            selection.execution_manifest,
            tasks,
            descriptor,
            arm=arm,
            selection=selection,
        )._planned_keys(selection.execution_manifest.arm_source_revisions[arm])
        for arm in ("baseline", "candidate")
    }

    assert sum(map(len, coordinator._schedule.values())) == 23
    assert {arm: len(keys) for arm, keys in planned_by_arm.items()} == {"baseline": 13, "candidate": 10}
    assert all(
        selection.contains(key.arm, key.model_class, key.transport, key.task_id, key.repeat_index)
        for keys in planned_by_arm.values()
        for key in keys.values()
    )
    selection_runner = BenchmarkRunner(
        selection.execution_manifest,
        tasks,
        descriptor,
        arm="baseline",
        selection=selection,
    )
    normal_runner = BenchmarkRunner(selection.execution_manifest, tasks, descriptor, arm="baseline")
    assert selection_runner._should_run_smoke_gate({"primary:http": [object()]}) is False
    assert normal_runner._should_run_smoke_gate({"primary:http": [object()]}) is True


def test_selection_summary_reads_the_selected_openrouter_upstream() -> None:
    outcome = TrialOutcome(
        task_id="read-vaults-en",
        category="single_operation",
        locale="en-US",
        arm="baseline",
        model_class="primary",
        model_id="deepseek/deepseek-v4-flash-0731",
        transport="http",
        provider_evidence=[
            {
                "provider_name": "openrouter",
                "routing": {
                    "endpoints": {
                        "available": [
                            {"provider": "DeepInfra", "selected": True},
                            {"provider": "StreamLake", "selected": False},
                        ]
                    }
                },
            }
        ],
    )

    assert selection_module._provider_names(outcome) == ["DeepInfra"]


def _complete_diagnostic_artifacts(
    selection: selection_module.ToolGuidanceSelection,
    tasks: list[Any],
) -> dict[str, dict[str, Any]]:
    model_ids = {model.class_name: model.model_id for model in selection.execution_manifest.models}
    events: list[dict[str, Any]] = []
    runs: dict[str, dict[str, list[dict[str, Any]]]] = {
        "baseline": {},
        "candidate": {},
    }
    for sequence, trial in enumerate(selection.selected_trials, start=1):
        task = next(item for item in tasks if item.id == trial.task_id)
        outcome = TrialOutcome(
            task_id=trial.task_id,
            category=task.category,
            locale=task.locale,
            arm=trial.arm,
            model_class=trial.model_class,
            model_id=model_ids[trial.model_class],
            transport=trial.transport,
            repeat_index=trial.repeat_index,
            error="synthetic terminal provider error",
            failure_kind=trial.failure_kind,
        ).model_dump(mode="json")
        run_key = f"{trial.model_class}:{trial.transport}"
        runs[trial.arm].setdefault(run_key, []).append(outcome)
        events.append(
            {
                "sequence": sequence,
                "cell": run_key,
                "task_id": trial.task_id,
                "repeat_index": trial.repeat_index,
                "arm": trial.arm,
                "order_position": planned_arm_order(
                    trial.task_id,
                    trial.repeat_index,
                    selection.execution_manifest.paired_order_seed,
                ).index(trial.arm),
            }
        )
    paired_budget = {
        "cost_usd": 0.0,
        "new_execution_limit_usd": str(selection.budget.new_execution_limit_usd),
    }
    return {
        arm: {
            "arm": arm,
            "selection": selection.artifact_metadata(),
            "status": "incomplete",
            "runs": {key: {"trials": rows} for key, rows in runs[arm].items()},
            "smoke_gate": {"status": "not_run", "required_cells": [], "cells": []},
            "checkpoint": {
                "record_count": selection.expected_trials_by_arm[arm],
                "lifecycle": "finalized",
                "reserved_cost_usd": 0,
                "smoke_status": None,
                "smoke_cell_count": 0,
            },
            "paired_execution": {"complete": True, "events": events},
            "paired_budget_used": paired_budget,
        }
        for arm in ("baseline", "candidate")
    }


def test_selection_summary_counts_23_terminal_failures_without_smoke_or_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, tasks, selection = _write_selection(tmp_path, monkeypatch)
    artifacts = _complete_diagnostic_artifacts(selection, tasks)

    summary = selection_module.build_selection_summary(
        selection,
        artifacts,
        {"verdict": "inconclusive"},
        manifest=selection.execution_manifest,
        tasks=tasks,
    )

    assert summary["status"] == "complete"
    assert summary["adopt_eligible"] is False
    assert summary["expected_selected_trials_by_arm"] == {"baseline": 13, "candidate": 10}
    assert summary["expected_new_model_trials"] == 23
    assert summary["observed_execution_events"] == 23
    assert summary["observed_new_smoke_model_requests"] == 0
    assert summary["arms"]["baseline"]["execution_failure_trials"] == 13
    assert summary["arms"]["candidate"]["execution_failure_trials"] == 10


@pytest.mark.asyncio
async def test_cli_uses_parent_manifest_and_new_execution_budget_without_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _write_selection(tmp_path, monkeypatch)
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    captured: list[dict[str, Any]] = []

    class Runner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arm = kwargs["arm"]
            self.secrets: tuple[str, ...] = ()
            self.manifest = args[0]
            captured.append(kwargs)

        async def run(self) -> dict[str, Any]:
            assert all(model.routing.allow_fallbacks for model in self.manifest.models)
            return {
                "schema_version": 2,
                "status": "incomplete",
                "arm": self.arm,
                "runs": {},
                "smoke_gate": {"status": "not_run", "required_cells": [], "cells": []},
                "paired_execution": {"complete": False, "events": []},
                "checkpoint": {},
            }

    monkeypatch.setattr(cli_module, "load_inputs", lambda *_args: (manifest, tasks, descriptor))
    monkeypatch.setattr(cli_module, "BenchmarkRunner", Runner)
    args = SimpleNamespace(
        manifest=ROOT / "config" / "run.json",
        corpus=ROOT / "corpus" / "tasks.json",
        coverage=ROOT / "corpus" / "tool-coverage.json",
        baseline_descriptor=tmp_path / "baseline-descriptor.json",
        candidate_descriptor=tmp_path / "candidate-descriptor.json",
        baseline_output=tmp_path / "baseline-output.json",
        candidate_output=tmp_path / "candidate-output.json",
        comparison_output=tmp_path / "comparison.json",
        baseline_checkpoint=tmp_path / "baseline.child.checkpoint.json",
        candidate_checkpoint=tmp_path / "candidate.child.checkpoint.json",
        selection=selection.path,
        selection_summary_output=tmp_path / "selection-summary.json",
        resume=False,
    )

    result = await cli_module.run_paired(args)

    assert result == 1
    assert len(captured) == 2
    ledger = captured[0]["shared_ledger"]
    assert captured[1]["shared_ledger"] is ledger
    assert ledger.total_cost_limit_usd == Decimal("38.8038872648")
    assert ledger.requests == 0
    assert ledger.cost_usd == Decimal("0")
    assert all(item["selection"].sha256 == selection.sha256 for item in captured)
    assert json.loads(args.comparison_output.read_text(encoding="utf-8"))["verdict"] == "inconclusive"
    assert json.loads(args.selection_summary_output.read_text(encoding="utf-8"))["status"] == "incomplete"
