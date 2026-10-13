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
from mcp_catalog.contracts import hash_json, load_run_manifest, load_task_corpus
from mcp_catalog.execution import TrialOutcome
from mcp_catalog.runner import BenchmarkRunner, PairedArmCoordinator, planned_arm_order
from mcp_catalog.runtime import RuntimeDescriptor
from paired_artifact_factory import synthetic_model_input_evidence
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


def _manifest():
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    budget = manifest.budget.model_copy(
        update={
            "max_total_cost_usd": 50,
            "max_cost_per_trial_usd": 0.10,
            "request_timeout_seconds": 300,
        }
    )
    models = [
        model.model_copy(
            update={
                "routing": model.routing.model_copy(update={"order": ["streamlake"]}),
                "input_cost_per_million_usd": 0.088,
                "output_cost_per_million_usd": 0.264,
            }
        )
        if model.class_name == "primary"
        else model
        for model in manifest.models
    ]
    return manifest.model_copy(update={"budget": budget, "models": models})


def _selection_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[Any], selection_module.ToolGuidanceSelection]:
    manifest = _manifest()
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    selected = [
        {
            "model_class": model_class,
            "transport": transport,
            "task_id": task_id,
            "repeat_index": repeat_index,
            "role": role,
            "parent_lineage": "previous",
        }
        for (model_class, transport, task_id, repeat_index), role in selection_module._ROLE_BY_IDENTITY.items()
    ]
    parent_bindings: dict[str, dict[str, str]] = {}
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in ("previous_baseline", "previous_candidate"):
        arm = "baseline" if name.endswith("_baseline") else "candidate"
        rows = []
        for trial in selected:
            rows.append(
                {
                    **{key: trial[key] for key in ("model_class", "transport", "task_id", "repeat_index")},
                    "arm": arm,
                    "success": not (arm == "candidate" and trial["role"] == "failure"),
                    "failure_kind": "none",
                }
            )
        artifact = {
            "arm": arm,
            "runs": {
                f"primary:{transport}": {
                    "trials": [row for row in rows if row["transport"] == transport]
                }
                for transport in ("http", "stdio")
            },
        }
        path = tmp_path / f"{name}.json"
        raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        parent_bindings[name] = {"path": str(path), "sha256": digest}
    monkeypatch.setattr(
        selection_module,
        "APPROVED_PARENT_SHA256",
        {name: parent["sha256"] for name, parent in parent_bindings.items()},
    )

    document = {
        "schema_version": 1,
        "authorization": selection_module.APPROVED_AUTHORIZATION.copy(),
        "parents": parent_bindings,
        "selected_trials": selected,
        "budget": {
            "session_max_usd": "50",
            "prior_conservative_spend_usd": "9.2804328442",
            "new_execution_limit_usd": "40.7195671558",
            "per_trial_usd": "0.10",
            "request_timeout_seconds": 300,
        },
    }
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    selection = selection_module.ToolGuidanceSelection.load(
        selection_path,
        manifest=manifest,
        tasks=tasks,
    )
    return manifest, tasks, selection


def test_approved_selection_pins_latest_authority_two_parents_and_14_identities() -> None:
    assert selection_module.APPROVED_AUTHORIZATION == {
        "uri": "akb://reef-akb/coll/issues/doc/akb-361.md",
        "revision": "41ff509113638ffe9ec0af1f803b42439be87405",  # pragma: allowlist secret
        "content_sha256": "b11b8f36d5c8fa36fe3eb6b453a56b612b15487f3d7b26b939d903c7b9f357a7",  # pragma: allowlist secret
    }
    assert selection_module.APPROVED_PARENT_SHA256 == {
        "previous_baseline": "276e0dfdfdad58ce083f65b3ea0c21a22675f31d8a393995dace62b77eaa43a4",  # pragma: allowlist secret
        "previous_candidate": "3195db132b810dfd37b58a4e1d8169ba92dca965ffa5140ec5c2a4624f6d647c",  # pragma: allowlist secret
    }
    assert len(selection_module._ROLE_BY_IDENTITY) == 14
    assert Counter(selection_module._ROLE_BY_IDENTITY.values()) == Counter(failure=6, control=8)
    assert all(identity[2] not in {"identity-access-ko", "stdio-local-en"} for identity in selection_module._ROLE_BY_IDENTITY)


def test_selection_validates_all_pinned_parent_rows_without_writing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    parent_bytes = {name: Path(item["path"]).read_bytes() for name, item in selection.parents.items()}
    loaded = selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)

    assert loaded.sha256 == selection.sha256
    assert len(loaded.selected_trials) == 14
    assert sum(trial.role == "failure" for trial in loaded.selected_trials) == 6
    assert sum(trial.role == "control" for trial in loaded.selected_trials) == 8
    assert set(loaded.parents) == {"previous_baseline", "previous_candidate"}
    assert selection.artifact_metadata()["expected_model_trials"] == 28
    preflight = selection.checkpoint_preflight()
    assert preflight["read_only"] is True
    assert preflight["selection_path"] == str(selection.path)
    assert preflight["selection_sha256"] == selection.sha256
    assert preflight["parent_count"] == 2
    assert preflight["selected_trial_count"] == 14
    assert preflight["role_counts"] == {"control": 8, "failure": 6}
    assert preflight["budget"] == {
        "session_max_usd": "50",
        "prior_conservative_spend_usd": "9.2804328442",
        "new_execution_limit_usd": "40.7195671558",
        "per_trial_usd": "0.10",
        "request_timeout_seconds": 300,
    }
    assert {name: Path(item["path"]).read_bytes() for name, item in loaded.parents.items()} == parent_bytes


def test_selection_preserves_operator_trial_order_without_requiring_a_canonical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    document = json.loads(selection.path.read_text(encoding="utf-8"))
    document["selected_trials"].reverse()
    selection.path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    loaded = selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)

    assert [trial.identity for trial in loaded.selected_trials] == [
        (row["model_class"], row["transport"], row["task_id"], row["repeat_index"])
        for row in document["selected_trials"]
    ]


def test_selection_summary_marks_model_input_hash_drift_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    trial = selection.selected_trials[0]
    task = next(item for item in tasks if item.id == trial.task_id)
    model_id = next(item.model_id for item in manifest.models if item.class_name == trial.model_class)
    evidence = synthetic_model_input_evidence(1)[0]
    bad_evidence = evidence.model_copy(
        update={"model_input_canonical_json": evidence.model_input_canonical_json + " "}
    )
    row = TrialOutcome(
        task_id=trial.task_id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class=trial.model_class,
        model_id=model_id,
        transport=trial.transport,
        repeat_index=trial.repeat_index,
        model_requests=1,
        model_input_evidence=[bad_evidence],
    ).model_dump(mode="json")
    expected_smoke_cells = [
        f"{model.class_name}:{transport}"
        for model in manifest.models
        for transport in manifest.transports
    ]
    artifacts = {
        arm: {
            "selection": selection.artifact_metadata(),
            "status": "complete",
            "runs": {f"{trial.model_class}:{trial.transport}": {"trials": [row] if arm == "baseline" else []}},
            "smoke_gate": {
                "status": "passed",
                "required_cells": expected_smoke_cells,
                "cells": (
                    [{"cell": expected_smoke_cells[0], "status": "completed", "outcome": row}]
                    if arm == "baseline"
                    else []
                ),
            },
        }
        for arm in ("baseline", "candidate")
    }

    summary = selection_module.build_selection_summary(
        selection,
        artifacts,
        {"verdict": "inconclusive"},
        manifest=manifest,
        tasks=tasks,
    )

    assert summary["arms"]["baseline"]["malformed_trials"] == 1
    assert summary["observed_new_smoke_model_requests"] == 1


def test_failure_role_requires_candidate_failure_without_constraining_failure_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    candidate_path = Path(selection.parents["previous_candidate"]["path"])
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate_failures = [
        row
        for run in candidate["runs"].values()
        for row in run["trials"]
        if not row["success"]
    ]
    assert candidate_failures
    candidate_failures[0]["failure_kind"] = "request_limit"
    raw = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    candidate_path.write_bytes(raw)
    document = json.loads(selection.path.read_text(encoding="utf-8"))
    document["parents"]["previous_candidate"]["sha256"] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        selection_module,
        "APPROVED_PARENT_SHA256",
        {
            "previous_baseline": selection.parents["previous_baseline"]["sha256"],
            "previous_candidate": hashlib.sha256(raw).hexdigest(),
        },
    )
    selection.path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)

    candidate_failures[0]["success"] = True
    raw = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    candidate_path.write_bytes(raw)
    document = json.loads(selection.path.read_text(encoding="utf-8"))
    document["parents"]["previous_candidate"]["sha256"] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        selection_module,
        "APPROVED_PARENT_SHA256",
        {
            "previous_baseline": selection.parents["previous_baseline"]["sha256"],
            "previous_candidate": hashlib.sha256(raw).hexdigest(),
        },
    )
    selection.path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="prior candidate failure"):
        selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)


def test_selection_requires_the_approved_streamlake_primary_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    assert next(model for model in manifest.models if model.class_name == "primary").routing.order == ["streamlake"]
    assert next(model for model in manifest.models if model.class_name == "lightweight").routing.order == ["akashml"]

    for model_class, wrong_provider in (("primary", "deepinfra"), ("lightweight", "streamlake")):
        wrong_models = [
            model.model_copy(update={"routing": model.routing.model_copy(update={"order": [wrong_provider]})})
            if model.class_name == model_class
            else model
            for model in manifest.models
        ]
        wrong_manifest = manifest.model_copy(update={"models": wrong_models})
        with pytest.raises(ValueError, match="approved fixed provider routes"):
            selection_module.ToolGuidanceSelection.load(
                selection.path,
                manifest=wrong_manifest,
                tasks=tasks,
            )

    for field, value in (("paired_order", "independent"), ("paired_order_seed", "obsolete-seed")):
        with pytest.raises(ValueError, match="approved paired arm order and seed"):
            selection_module.ToolGuidanceSelection.load(
                selection.path,
                manifest=manifest.model_copy(update={field: value}),
                tasks=tasks,
            )


def test_relative_parent_paths_resolve_from_the_selection_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    document = json.loads(selection.path.read_text(encoding="utf-8"))
    for binding in document["parents"].values():
        binding["path"] = Path(binding["path"]).name
    selection.path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)

    loaded = selection_module.ToolGuidanceSelection.load(
        selection.path,
        manifest=manifest,
        tasks=tasks,
    )

    assert all(Path(parent["path"]).is_absolute() for parent in loaded.parents.values())


def test_selection_requires_the_approved_plan_and_budget_in_the_operator_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    document = json.loads(selection.path.read_text(encoding="utf-8"))
    assert set(document) == {"schema_version", "authorization", "parents", "selected_trials", "budget"}
    assert selection.budget.session_max_usd == Decimal("50")
    assert selection.budget.prior_conservative_spend_usd == Decimal("9.2804328442")
    assert selection.budget.new_execution_limit_usd == Decimal("40.7195671558")

    document["budget"]["prior_conservative_spend_usd"] = "9.0583979192"
    document["budget"]["new_execution_limit_usd"] = "40.9416020808"
    selection.path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="selection file is invalid"):
        selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("authorization", "revision", "0" * 40, "approved AKB-361 revision"),
        ("parents", "previous_candidate", {"path": "candidate.json", "sha256": "c" * 64}, "parent artifact hashes"),
    ],
)
def test_selection_rejects_unapproved_authority_or_parent_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: Any,
    message: str,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    document = json.loads(selection.path.read_text(encoding="utf-8"))
    if section == "authorization":
        document[section][field] = value
    else:
        document[section][field] = value
    selection.path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "missing", "extra", "out_of_range", "wrong_role", "wrong_lineage"],
)
def test_selection_rejects_identity_or_role_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    document = json.loads(selection.path.read_text(encoding="utf-8"))
    if mutation == "duplicate":
        document["selected_trials"].append(document["selected_trials"][0])
    elif mutation == "missing":
        document["selected_trials"].pop()
    elif mutation == "extra":
        document["selected_trials"][0]["task_id"] = "unknown-task"
    elif mutation == "out_of_range":
        document["selected_trials"][0]["repeat_index"] = 3
    elif mutation == "wrong_role":
        document["selected_trials"][0]["role"] = "control"
    else:
        document["selected_trials"][0]["parent_lineage"] = "original"
    selection.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)


@pytest.mark.parametrize("mutation", ["old16", "four_parents", "old_budget", "old_revision"])
def test_selection_rejects_obsolete_plan_authority_and_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    document = json.loads(selection.path.read_text(encoding="utf-8"))
    if mutation == "old16":
        document["selected_trials"].extend(
            [
                {
                    "model_class": "primary",
                    "transport": "stdio",
                    "task_id": task_id,
                    "repeat_index": 2,
                    "role": "failure",
                    "parent_lineage": "previous",
                }
                for task_id in ("identity-access-ko", "stdio-local-en")
            ]
        )
    elif mutation == "four_parents":
        document["parents"].update(
            {
                "recovery_baseline": {"path": "recovery-baseline.json", "sha256": "a" * 64},
                "recovery_candidate": {"path": "recovery-candidate.json", "sha256": "b" * 64},
            }
        )
    elif mutation == "old_budget":
        document["budget"].update(
            {
                "prior_conservative_spend_usd": "9.1584802508",
                "new_execution_limit_usd": "40.8415197492",
            }
        )
    else:
        document["authorization"].update(
            {
                "revision": "774621eccd157d955d489d50d8a9f5177d604ce1",  # pragma: allowlist secret
                "content_sha256": "97b77a3514ef4e3a3c4a142508f750b1dc5d192514a5b173c54db87546f9bf7d",  # pragma: allowlist secret
            }
        )
    selection.path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ValueError):
        selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)


def test_selection_rejects_parent_drift_and_budget_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    parent = Path(selection.parents["previous_baseline"]["path"])
    parent.write_bytes(parent.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        selection_module.ToolGuidanceSelection.load(selection.path, manifest=manifest, tasks=tasks)

    _manifest_again, _tasks_again, second = _selection_fixture(tmp_path / "budget", monkeypatch)
    for field, value in (
        ("max_total_cost_usd", 49),
        ("max_cost_per_trial_usd", 0.11),
        ("request_timeout_seconds", 301),
    ):
        wrong_budget = manifest.budget.model_copy(update={field: value})
        with pytest.raises(ValueError, match="measurement budget"):
            selection_module.ToolGuidanceSelection.load(
                second.path,
                manifest=manifest.model_copy(update={"budget": wrong_budget}),
                tasks=tasks,
            )


def test_selection_filters_schedules_and_hashes_its_pre_smoke_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    runner = BenchmarkRunner(manifest, tasks, descriptor, selection=selection)
    planned = runner._planned_keys(manifest.arm_source_revisions["baseline"])
    coordinator = PairedArmCoordinator(manifest, tasks, selection=selection)

    assert len(planned) == 14
    assert sum(map(len, coordinator._schedule.values())) == 28
    assert all(selection.contains(key.model_class, key.transport, key.task_id, key.repeat_index) for key in planned.values())
    assert coordinator.evidence()["seed"] == manifest.paired_order_seed
    assert coordinator.evidence()["mode"] == "counterbalanced_task_repeat"
    expected_schedule = {}
    for model in manifest.models:
        for transport in manifest.transports:
            cell = f"{model.class_name}:{transport}"
            expected_schedule[cell] = [
                (task.id, repeat_index, arm)
                for repeat_index in range(1, manifest.repeats + 1)
                for task in tasks
                if transport in task.fixture.transports
                and selection.contains(model.class_name, transport, task.id, repeat_index)
                for arm in planned_arm_order(task.id, repeat_index, manifest.paired_order_seed)
            ]
    assert coordinator._schedule == expected_schedule
    runtime = {"source_revision": manifest.arm_source_revisions["baseline"], "artifact_versions": {}}
    seal = runner._local_pre_smoke_seal(runtime, {})
    assert seal["selection"]["selection_sha256"] == selection.sha256
    assert seal["selection"]["authorization"] == selection.authorization.model_dump(mode="json")
    assert seal["selection"]["selected_identity_count"] == 14
    assert seal["selection"]["expected_model_trials"] == 28
    assert seal["selection"]["budget"]["prior_conservative_spend_usd"] == "9.2804328442"
    assert seal["selection"]["budget"]["new_execution_limit_usd"] == "40.7195671558"
    assert set(seal["selection"]["parents"]) == {"previous_baseline", "previous_candidate"}
    assert seal["paired_order_mode"] == manifest.paired_order
    assert seal["paired_order_seed"] == manifest.paired_order_seed
    for trial in selection.selected_trials:
        scheduled_order = next(
            item["arm_order"]
            for item in seal["paired_order_plan"]
            if item["task_id"] == trial.task_id and item["repeat_index"] == trial.repeat_index
        )
        assert scheduled_order == list(planned_arm_order(trial.task_id, trial.repeat_index, manifest.paired_order_seed))
    changed = selection.__class__(
        path=selection.path,
        sha256="f" * 64,
        authorization=selection.authorization,
        budget=selection.budget,
        parents=selection.parents,
        selected_trials=selection.selected_trials,
        parent_results=selection.parent_results,
    )
    changed_seal = BenchmarkRunner(manifest, tasks, descriptor, selection=changed)._local_pre_smoke_seal(runtime, {})
    assert hash_json(seal) != hash_json(changed_seal)


@pytest.mark.asyncio
async def test_selection_resume_restores_contiguous_terminal_arm_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    coordinator = PairedArmCoordinator(manifest, tasks, selection=selection)
    reused: dict[str, dict[tuple[str, str, int], TrialOutcome]] = {"baseline": {}, "candidate": {}}
    sequence = 0
    model_ids = {item.class_name: item.model_id for item in manifest.models}
    for cell, schedule in coordinator._schedule.items():
        model_class, transport = cell.split(":", 1)
        for task_id, repeat_index, arm in schedule:
            sequence += 1
            order = planned_arm_order(task_id, repeat_index, manifest.paired_order_seed)
            reused[arm][(cell, task_id, repeat_index)] = TrialOutcome(
                task_id=task_id,
                category="single_operation",
                arm=arm,
                model_class=model_class,
                model_id=model_ids[model_class],
                transport=transport,
                repeat_index=repeat_index,
                paired_order_position=order.index(arm),
                paired_execution_sequence=sequence,
            )

    await coordinator.register_arm("baseline", reused["baseline"])
    await coordinator.register_arm("candidate", reused["candidate"])

    assert len(coordinator.evidence()["events"]) == 28
    assert coordinator.evidence()["complete"] is True


@pytest.mark.asyncio
async def test_cli_uses_new_selection_ledger_and_writes_separate_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _selection_fixture(tmp_path, monkeypatch)
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    captured: list[dict[str, Any]] = []

    class Runner:
        def __init__(self, *_args: object, **kwargs: Any) -> None:
            self.arm = kwargs["arm"]
            self.secrets: tuple[str, ...] = ()
            captured.append(kwargs)

        async def run(self) -> dict[str, Any]:
            return {
                "schema_version": 2,
                "status": "incomplete",
                "arm": self.arm,
                "runs": {},
                "smoke_gate": {},
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
        baseline_checkpoint=tmp_path / "baseline.checkpoint.json",
        candidate_checkpoint=tmp_path / "candidate.checkpoint.json",
        selection=selection.path,
        selection_summary_output=tmp_path / "selection-summary.json",
        resume=False,
    )

    result = await cli_module.run_paired(args)

    assert result == 1
    assert len(captured) == 2
    ledger = captured[0]["shared_ledger"]
    assert captured[1]["shared_ledger"] is ledger
    assert ledger.total_cost_limit_usd == Decimal("40.7195671558")
    assert ledger.requests == 0
    assert ledger.cost_usd == Decimal("0")
    assert all(item["selection"].sha256 == selection.sha256 for item in captured)
    assert json.loads(args.comparison_output.read_text(encoding="utf-8"))["verdict"] == "inconclusive"
    summary = json.loads(args.selection_summary_output.read_text(encoding="utf-8"))
    assert summary["status"] == "incomplete"
    assert summary["adopt_eligible"] is False
    assert summary["expected_new_model_trials"] == 28
