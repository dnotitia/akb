from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_catalog.cli as cli_module
import mcp_catalog.recovery as recovery_module
from mcp_catalog.contracts import hash_json, load_run_manifest, load_task_corpus
from mcp_catalog.recovery import RecoverySelection, build_recovery_summary
from mcp_catalog.runner import BenchmarkRunner, PairedArmCoordinator, compare_artifacts, planned_arm_order
from mcp_catalog.runtime import RuntimeDescriptor
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


def _recovery_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[Any], RecoverySelection]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    task = next(task for task in tasks if "http" in task.fixture.transports)
    trial = {
        "model_class": "primary",
        "transport": "http",
        "task_id": task.id,
        "repeat_index": 1,
    }
    prior_manifest = manifest.model_dump(mode="json")
    for model in prior_manifest["models"]:
        model["routing"] = {"order": ["parasail"], "allow_fallbacks": False}

    parent_bindings: dict[str, dict[str, str]] = {}
    for arm in ("baseline", "candidate"):
        model = next(model for model in manifest.models if model.class_name == "primary")
        parent_artifact = {
            "arm": arm,
            "manifest": prior_manifest,
            "run_manifest_hash": hash_json(prior_manifest),
            "source_revision": manifest.arm_source_revisions[arm],
            "task_corpus_hash": hash_json([item.model_dump(mode="json") for item in tasks]),
            "task_ids": [item.id for item in tasks],
            "task_contracts": [item.model_dump(mode="json") for item in tasks],
            "runs": {
                "primary:http": {
                    "trials": [
                        {
                            **trial,
                            "category": task.category,
                            "locale": task.locale,
                            "arm": arm,
                            "model_id": model.model_id,
                            "error": "provider returned HTTP 503",
                            "failure_kind": "provider",
                        }
                    ]
                }
            },
        }
        raw = json.dumps(parent_artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        parent_path = tmp_path / f"{arm}.json"
        parent_path.write_bytes(raw)
        parent_bindings[arm] = {"path": str(parent_path), "sha256": hashlib.sha256(raw).hexdigest()}

    document = {
        "schema_version": 1,
        "mode": "paired_provider_recovery",
        "parents": parent_bindings,
        "selection_basis": recovery_module.SELECTION_BASIS,
        "selected_trials": [trial],
        "unmeasured_parent_provider_outcomes": [{"arm": "baseline", **trial}],
    }
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    selection_path = tmp_path / "selection.json"
    selection_path.write_bytes(raw)
    monkeypatch.setattr(recovery_module, "AUTHORIZED_SELECTION_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(recovery_module, "EXPECTED_SELECTED_IDENTITIES", 1)
    monkeypatch.setattr(recovery_module, "EXPECTED_UNMEASURED_PARENT_OUTCOMES", 1)
    selection = RecoverySelection.load(selection_path, manifest=manifest, tasks=tasks)
    return manifest, tasks, selection


def test_pinned_selection_validates_parent_hashes_and_records_without_writing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _recovery_fixture(tmp_path, monkeypatch)
    parent_bytes = {arm: Path(binding["path"]).read_bytes() for arm, binding in selection.parents.items()}

    loaded = RecoverySelection.load(selection.path, manifest=manifest, tasks=tasks)

    assert loaded.sha256 == selection.sha256
    assert len(loaded.selected_trials) == 1
    assert len(loaded.unmeasured_parent_provider_outcomes) == 1
    assert {arm: Path(binding["path"]).read_bytes() for arm, binding in loaded.parents.items()} == parent_bytes


def test_selection_and_parent_hashes_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, tasks, selection = _recovery_fixture(tmp_path, monkeypatch)
    selection.path.write_bytes(selection.path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="authorized pinned file"):
        RecoverySelection.load(selection.path, manifest=manifest, tasks=tasks)

    _manifest, _tasks, second = _recovery_fixture(tmp_path / "second", monkeypatch)
    parent = Path(second.parents["candidate"]["path"])
    parent.write_bytes(parent.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        RecoverySelection.load(second.path, manifest=manifest, tasks=tasks)


def test_selection_filters_trials_but_keeps_full_smoke_seal_and_paired_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _recovery_fixture(tmp_path, monkeypatch)
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    runner = BenchmarkRunner(manifest, tasks, descriptor, recovery_selection=selection)
    planned = runner._planned_keys(manifest.arm_source_revisions["baseline"])
    coordinator = PairedArmCoordinator(manifest, tasks, recovery_selection=selection)

    assert len(planned) == 1
    key = next(iter(planned.values()))
    identity = (key.model_class, key.transport, key.task_id, key.repeat_index)
    assert identity in selection.identities
    assert sum(len(events) for events in coordinator._schedule.values()) == 2
    assert coordinator._schedule[f"{key.model_class}:{key.transport}"] == [
        (key.task_id, key.repeat_index, arm)
        for arm in planned_arm_order(key.task_id, key.repeat_index, manifest.paired_order_seed)
    ]

    runtime = {"source_revision": manifest.arm_source_revisions["baseline"], "artifact_versions": {}}
    seal = runner._local_pre_smoke_seal(runtime, {})
    assert len(seal["paired_order_plan"]) == manifest.repeats * len(tasks)
    assert seal["recovery_selection"]["selection_sha256"] == selection.sha256
    changed_selection = dataclasses.replace(selection, sha256="f" * 64)
    changed_runner = BenchmarkRunner(manifest, tasks, descriptor, recovery_selection=changed_selection)
    changed_seal = changed_runner._local_pre_smoke_seal(runtime, {})
    assert hash_json(seal) != hash_json(changed_seal)


def test_recovery_comparison_is_always_inconclusive_and_summary_requires_smoke_and_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _recovery_fixture(tmp_path, monkeypatch)
    metadata = selection.artifact_metadata()
    comparison = compare_artifacts(
        {"arm": "baseline", "recovery_selection": metadata},
        {"arm": "candidate", "recovery_selection": metadata},
    )
    assert comparison["verdict"] == "inconclusive"

    summary = build_recovery_summary(
        selection,
        {
            "baseline": {"status": "complete", "recovery_selection": metadata, "runs": {}},
            "candidate": {"status": "complete", "recovery_selection": metadata, "runs": {}},
        },
        comparison,
        manifest=manifest,
        tasks=tasks,
    )
    assert summary["status"] == "incomplete"
    assert summary["canonical_comparison_verdict"] == "inconclusive"
    assert summary["expected_paired_identities"] == 1
    assert summary["expected_new_model_trials"] == 2
    assert summary["smoke_cells_per_arm"] == 4
    assert summary["new_execution_cost_limit_usd"] == "41.89781216"


@pytest.mark.asyncio
async def test_paired_selection_uses_fresh_shared_recovery_ledger_and_separate_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _recovery_fixture(tmp_path, monkeypatch)
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    captured: list[dict[str, Any]] = []

    class Runner:
        def __init__(self, *_args: object, **kwargs: Any) -> None:
            self.arm = kwargs["arm"]
            self.secrets: tuple[str, ...] = ()
            captured.append(kwargs)

        async def run(self) -> dict[str, Any]:
            return {"schema_version": 2, "status": "incomplete", "arm": self.arm, "runs": {}, "smoke_gate": {}}

    monkeypatch.setattr(cli_module, "load_inputs", lambda *_args: (manifest, tasks, descriptor))
    monkeypatch.setattr(cli_module, "RecoverySelection", SimpleNamespace(load=lambda *_args, **_kwargs: selection))
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
        baseline_checkpoint=tmp_path / "baseline.checkpoint.json",
        candidate_checkpoint=tmp_path / "candidate.checkpoint.json",
        selection=selection.path,
        recovery_summary_output=tmp_path / "recovery-summary.json",
        resume=False,
    )

    result = await cli_module.run_paired(args)

    assert result == 1
    assert len(captured) == 2
    ledgers = [item["shared_ledger"] for item in captured]
    assert ledgers[0] is ledgers[1]
    assert ledgers[0].total_cost_limit_usd == Decimal("41.89781216")
    assert ledgers[0].requests == 0
    assert ledgers[0].cost_usd == Decimal("0")
    assert all(item["recovery_selection"] is selection for item in captured)
    assert sum(len(events) for events in captured[0]["paired_coordinator"]._schedule.values()) == 2
    assert json.loads(args.comparison_output.read_text(encoding="utf-8"))["verdict"] == "inconclusive"
    summary = json.loads(args.recovery_summary_output.read_text(encoding="utf-8"))
    assert summary["status"] == "incomplete"
    assert summary["parent_results_merged"] is False
