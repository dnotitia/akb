from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import mcp_catalog.cli as cli_module
import mcp_catalog.execution as execution_module
import mcp_catalog.runner as runner_module
import mcp_catalog.selection as selection_module
from mcp_catalog.checkpoint import (
    CheckpointHeader,
    CheckpointKey,
    CheckpointPairedEvent,
    CheckpointStore,
    checkpoint_key_digest,
    checkpoint_record_hash,
)
from mcp_catalog.contracts import hash_json, load_run_manifest, load_task_corpus
from mcp_catalog.evidence import canonical_json
from mcp_catalog.execution import ModelInputEvidence, TrialOutcome
from mcp_catalog.pairing import planned_arm_order
from mcp_catalog.runner import BenchmarkRunner, PairedArmCoordinator
from mcp_catalog.runtime import RuntimeDescriptor
from paired_artifact_factory import _catalog_snapshot, provider_registry_snapshot
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


@pytest.mark.asyncio
async def test_selection_resume_preserves_unknown_gap_and_retries_interrupted_trials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, tasks, selection = _write_selection(tmp_path, monkeypatch)
    manifest = selection.execution_manifest
    task_by_id = {task.id: task for task in tasks}
    model_by_class = {model.class_name: model for model in manifest.models}
    per_arm = {
        arm: [trial for trial in selection.selected_trials if trial.arm == arm]
        for arm in ("baseline", "candidate")
    }
    completed = [per_arm["baseline"][0], per_arm["baseline"][1], per_arm["candidate"][0]]
    interrupted = [per_arm["baseline"][2], per_arm["candidate"][1]]
    source_rows = [
        (completed[0], 1, "completed"),
        (completed[1], 2, "completed"),
        (completed[2], 3, "completed"),
        (interrupted[0], 5, "failed"),
        (interrupted[1], 6, "failed"),
    ]
    history: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
    reused: dict[str, dict[tuple[str, str, int], TrialOutcome]] = {
        "baseline": {},
        "candidate": {},
    }
    interrupted_identities = {
        (trial.arm, trial.model_class, trial.transport, trial.task_id, trial.repeat_index)
        for trial in interrupted
    }
    for trial, sequence, status in source_rows:
        task = task_by_id[trial.task_id]
        cell = f"{trial.model_class}:{trial.transport}"
        order_position = planned_arm_order(
            trial.task_id,
            trial.repeat_index,
            manifest.paired_order_seed,
        ).index(trial.arm)
        history[trial.arm].append(
            {
                "sequence": sequence,
                "cell": cell,
                "task_id": trial.task_id,
                "repeat_index": trial.repeat_index,
                "arm": trial.arm,
                "order_position": order_position,
                "attempt": 1,
                "status": status,
            }
        )
        if (trial.arm, trial.model_class, trial.transport, trial.task_id, trial.repeat_index) in interrupted_identities:
            continue
        outcome = TrialOutcome(
            task_id=trial.task_id,
            category=task.category,
            locale=task.locale,
            arm=trial.arm,
            model_class=trial.model_class,
            model_id=model_by_class[trial.model_class].model_id,
            transport=trial.transport,
            repeat_index=trial.repeat_index,
            paired_order_position=order_position,
            paired_execution_sequence=sequence,
        )
        reused[trial.arm][(cell, trial.task_id, trial.repeat_index)] = outcome

    coordinator = PairedArmCoordinator(manifest, tasks, selection=selection)
    await coordinator.register_arm("baseline", reused["baseline"], history_events=history["baseline"])
    await coordinator.register_arm("candidate", reused["candidate"], history_events=history["candidate"])
    assert coordinator._unknown_start_gaps == [{"sequence": 4, "status": "unknown_start"}]

    retry_links: dict[tuple[str, str, str, str, int], int] = {}
    new_trials = 0
    for cell, schedule in coordinator._schedule.items():
        model_class, transport = cell.split(":", 1)
        for task_id, repeat_index, arm in schedule:
            identity = (arm, model_class, transport, task_id, repeat_index)
            if (cell, task_id, repeat_index) in reused[arm]:
                continue
            task = task_by_id[task_id]
            async with coordinator.turn(
                cell=cell,
                task_id=task_id,
                repeat_index=repeat_index,
                arm=arm,
            ) as turn:
                if identity in interrupted_identities:
                    retry_links[identity] = turn.retry_of_sequence or 0
                    assert turn.attempt == 2
                else:
                    assert turn.attempt == 1
                outcome = TrialOutcome(
                    task_id=task_id,
                    category=task.category,
                    locale=task.locale,
                    arm=arm,
                    model_class=model_class,
                    model_id=model_by_class[model_class].model_id,
                    transport=transport,
                    repeat_index=repeat_index,
                    paired_order_position=turn.order_position,
                    paired_execution_sequence=turn.execution_sequence,
                )
                coordinator.mark_terminal_outcome(outcome, "failed")
                new_trials += 1

    evidence = coordinator.evidence()
    events = evidence["events"]
    retry_events = [event for event in events if event.get("attempt") == 2]
    assert new_trials == 20
    assert len(events) == 25
    assert len({(event["cell"], event["task_id"], event["repeat_index"], event["arm"]) for event in events}) == 23
    assert [event["sequence"] for event in events] == [1, 2, 3, *range(5, 27)]
    assert evidence["unknown_start_gaps"] == [{"sequence": 4, "status": "unknown_start"}]
    assert len(retry_events) == 2
    assert {event["retry_of_sequence"] for event in retry_events} == {5, 6}
    assert retry_links == {
        (trial.arm, trial.model_class, trial.transport, trial.task_id, trial.repeat_index): sequence
        for trial, sequence in ((interrupted[0], 5), (interrupted[1], 6))
    }
    assert evidence["unfinished_starts"] == []
    assert evidence["complete"] is True


@pytest.mark.asyncio
async def test_selection_run_passes_captured_catalog_evidence_through_real_runner_without_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, tasks, selection = _write_selection(tmp_path / "parents", monkeypatch)
    parent_bytes = {
        name: Path(parent["path"]).read_bytes()
        for name, parent in selection.parents.items()
    }
    pinned_manifest = selection.execution_manifest
    descriptors: dict[str, RuntimeDescriptor] = {}
    for arm in ("baseline", "candidate"):
        raw = descriptor_dict()
        raw["evidence"]["source_revision"] = pinned_manifest.arm_source_revisions[arm]
        descriptor = RuntimeDescriptor.from_dict(raw)
        cells = {
            key: dataclasses.replace(descriptor)
            for key in ("primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio")
        }
        descriptors[arm] = dataclasses.replace(descriptor, benchmark_cells=cells)

    class Resolver:
        def required_profiles(self, selected_tasks):
            return sorted({task.fixture.credential_profile for task in selected_tasks})

        async def prepare(self, _fixture, _profiles) -> None:
            return None

        def token_for(self, _profile: str) -> str:
            return "fixture-token"

        def secrets_for(self, _profile: str) -> tuple[str, ...]:
            return ("fixture-token",)

        def secret_values(self) -> tuple[str, ...]:
            return ("fixture-token",)

        async def refresh_after_reset(self, _fixture, _profile: str) -> str:
            return "fresh-fixture-token"

        def mark_reset_complete(self) -> None:
            return None

        async def cleanup(self, _fixture) -> None:
            return None

    class Fixture:
        def __init__(self, descriptor) -> None:
            self.descriptor = descriptor
            self.reset_calls = 0

        async def wait_until_ready(self, *, stage: str) -> None:
            assert stage == "runtime_readiness"

        async def reset(self) -> None:
            self.reset_calls += 1

        async def close(self) -> None:
            return None

        def reset_evidence(self) -> dict[str, float | int]:
            return {"count": self.reset_calls, "wall_seconds": 0.0}

    class Executor:
        def __init__(self, selected_manifest, **kwargs: Any) -> None:
            self.manifest = selected_manifest
            self.arm = kwargs["arm"]
            self.model_spec = kwargs["model_spec"]
            self.transport = kwargs["transport"]
            self.input_schemas_by_profile = kwargs["input_schemas_by_profile"]
            self.server_instructions_by_profile = kwargs["server_instructions_by_profile"]
            self.paired_order_position: int | None = None
            self.paired_execution_sequence: int | None = None
            captured_cells[(self.arm, self.model_spec.class_name, self.transport)] = {
                "schemas": self.input_schemas_by_profile,
                "instructions": self.server_instructions_by_profile,
            }

        def set_repeat_index(self, _repeat_index: int) -> None:
            return None

        def set_paired_turn(self, *, order_position: int, execution_sequence: int) -> None:
            self.paired_order_position = order_position
            self.paired_execution_sequence = execution_sequence

        def clear_paired_turn(self) -> None:
            return None

    class Report:
        def __init__(self, outcomes: list[TrialOutcome]) -> None:
            self.failures: list[object] = []
            self.cases = [SimpleNamespace(output=outcome) for outcome in outcomes]

    captured_cells: dict[tuple[str, str, str], dict[str, Any]] = {}
    captured_inputs: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
    smoke_calls: list[str] = []
    model_calls: list[str] = []

    async def fake_preflight(runner: BenchmarkRunner) -> dict[str, Any]:
        return {
            "runtime": {
                "source_revision": pinned_manifest.arm_source_revisions[runner.arm],
                "artifact_versions": {
                    "backend_artifact_version": "1.0.0",
                    "proxy_artifact_version": "1.0.0",
                },
                "discovery": {"status": "ready", "scenario": runner.descriptor.scenario},
            },
            "resolver": Resolver(),
        }

    async def fake_provider_registry(runner: BenchmarkRunner, _ledger: Any) -> dict[str, Any]:
        registry_models = [
            model.model_copy(
                update={
                    "routing": model.routing.model_copy(
                        update={"order": ["deepinfra" if model.class_name == "primary" else "akashml"]}
                    )
                }
            )
            for model in runner.manifest.models
        ]
        registry_manifest = runner.manifest.model_copy(update={"models": registry_models})
        registry = provider_registry_snapshot(registry_manifest)
        runner.provider_registry = registry
        return registry

    async def fake_capture(_fixture: Any, **kwargs: Any) -> Any:
        arm = next(
            arm
            for arm, revision in pinned_manifest.arm_source_revisions.items()
            if revision == kwargs["source_revision"]
        )
        snapshot = _catalog_snapshot(pinned_manifest, arm, kwargs["transport"])
        return snapshot.model_copy(
            update={
                "source_revision": kwargs["source_revision"],
                "artifact_version": kwargs["artifact_version"],
                "capability_profile": kwargs["capability_profile"],
            }
        )

    async def fake_evaluate(selected_tasks, *, executor, repeat_indices, checkpoint_sink, **_kwargs):
        outcomes: list[TrialOutcome] = []
        for task in selected_tasks:
            profile = task.fixture.credential_profile
            instructions = executor.server_instructions_by_profile[profile]
            schemas = executor.input_schemas_by_profile[profile]
            model_input = canonical_json(
                {"server_instructions": instructions, "tool_input_schemas": schemas}
            )
            evidence = ModelInputEvidence(
                request_index=1,
                model_input_sha256=hashlib.sha256(model_input.encode("utf-8")).hexdigest(),
                model_input_canonical_json=model_input,
                server_instructions_sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
                server_instructions_text=instructions,
                server_instructions_present=True,
            )
            repeat_index = repeat_indices[task.id]
            outcome = TrialOutcome(
                task_id=task.id,
                category=task.category,
                locale=task.locale,
                arm=executor.arm,
                model_class=executor.model_spec.class_name,
                model_id=executor.model_spec.model_id,
                transport=executor.transport,
                repeat_index=repeat_index,
                paired_order_position=executor.paired_order_position,
                paired_execution_sequence=executor.paired_execution_sequence,
                error="synthetic local-only model response",
                failure_kind="provider",
                model_requests=1,
                server_instructions_sha256=evidence.server_instructions_sha256,
                model_input_evidence=[evidence],
            )
            identity = (executor.arm, executor.model_spec.class_name, executor.transport, task.id, repeat_index)
            captured_inputs[identity] = {
                "server_instructions": instructions,
                "tool_input_schemas": schemas,
            }
            await checkpoint_sink(outcome, "failed")
            outcomes.append(outcome)
        return Report(outcomes)

    async def forbidden_smoke(*_args: Any, **_kwargs: Any) -> None:
        smoke_calls.append("called")
        raise AssertionError("selection path must not execute paid smoke")

    async def forbidden_model_request(*_args: Any, **_kwargs: Any) -> Any:
        model_calls.append("called")
        raise AssertionError("selection regression must not call a model provider")

    monkeypatch.setattr(cli_module, "load_inputs", lambda _manifest, _corpus, descriptor_path, _coverage: (
        manifest,
        tasks,
        descriptors["baseline" if descriptor_path == args.baseline_descriptor else "candidate"],
    ))
    monkeypatch.setattr(runner_module, "RuntimeFixture", Fixture)
    monkeypatch.setattr(BenchmarkRunner, "preflight", fake_preflight)
    monkeypatch.setattr(BenchmarkRunner, "_capture_provider_registry", fake_provider_registry)
    monkeypatch.setattr(runner_module, "capture_catalog", fake_capture)
    monkeypatch.setattr(runner_module, "build_model", lambda model_spec: SimpleNamespace(settings={}, model_spec=model_spec))
    monkeypatch.setattr(runner_module, "TrialExecutor", Executor)
    monkeypatch.setattr(runner_module, "evaluate_dataset", fake_evaluate)
    monkeypatch.setattr(runner_module, "serialize_report", lambda report, _secrets: {"case_count": len(report.cases)})
    monkeypatch.setattr(runner_module, "execute_smoke", forbidden_smoke)
    monkeypatch.setattr(execution_module, "run_agent_with_timeout", forbidden_model_request)

    args = SimpleNamespace(
        manifest=ROOT / "config" / "run.json",
        corpus=ROOT / "corpus" / "tasks.json",
        coverage=ROOT / "corpus" / "tool-coverage.json",
        baseline_descriptor=tmp_path / "baseline-descriptor.json",
        candidate_descriptor=tmp_path / "candidate-descriptor.json",
        baseline_output=tmp_path / "baseline-output.json",
        candidate_output=tmp_path / "candidate-output.json",
        comparison_output=tmp_path / "comparison-output.json",
        baseline_checkpoint=tmp_path / "baseline-checkpoint.json",
        candidate_checkpoint=tmp_path / "candidate-checkpoint.json",
        selection=selection.path,
        selection_summary_output=tmp_path / "selection-summary.json",
        amendment=None,
        resume=False,
    )
    result = await cli_module.run_paired(args)

    assert result == 0
    assert smoke_calls == []
    assert model_calls == []
    summary = json.loads(args.selection_summary_output.read_text(encoding="utf-8"))
    assert summary["status"] == "complete"
    assert summary["adopt_eligible"] is False
    assert summary["expected_new_model_trials"] == 23
    expected = {trial.identity for trial in selection.selected_trials}
    observed: set[tuple[str, str, str, str, int]] = set()
    for arm, path in (("baseline", args.baseline_output), ("candidate", args.candidate_output)):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        assert artifact["smoke_gate"] == {"status": "not_run", "required_cells": [], "cells": []}
        assert artifact["checkpoint"]["smoke_cell_count"] == 0
        assert artifact["checkpoint"]["smoke_status"] is None
        for run in artifact["runs"].values():
            for row in run["trials"]:
                outcome = TrialOutcome.model_validate(row)
                identity = (arm, outcome.model_class, outcome.transport, outcome.task_id, outcome.repeat_index)
                observed.add(identity)
                expected_input = captured_inputs[identity]
                recorded_input = json.loads(outcome.model_input_evidence[0].model_input_canonical_json)
                assert recorded_input == expected_input
                assert outcome.arm == arm
    assert observed == expected
    assert Counter(identity[0] for identity in observed) == Counter(baseline=13, candidate=10)
    assert len(captured_inputs) == 23
    assert captured_cells
    assert all(
        cell["schemas"] and cell["instructions"]
        for cell in captured_cells.values()
    )
    events = json.loads(args.baseline_output.read_text(encoding="utf-8"))["paired_execution"]
    assert events["complete"] is True
    assert len(events["events"]) == 23
    assert [event["sequence"] for event in events["events"]] == list(range(1, 24))
    assert {parent: Path(binding["path"]).read_bytes() for parent, binding in selection.parents.items()} == parent_bytes


@pytest.mark.asyncio
async def test_selection_resume_restores_each_arm_budget_once_and_reuses_terminal_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, tasks, selection = _write_selection(tmp_path / "parents", monkeypatch)
    manifest = selection.execution_manifest
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    task_by_id = {task.id: task for task in tasks}
    registry = {"status": "unavailable", "reason": "synthetic checkpoint fixture"}
    smoke_cells = {
        f"{model.class_name}:{transport}": (model.class_name, model.model_id, transport)
        for model in manifest.models
        for transport in manifest.transports
    }
    checkpoint_spend: dict[str, Any] = {}
    checkpoint_seals: dict[str, str] = {}
    sequence_by_identity = {
        trial.identity: sequence
        for sequence, trial in enumerate(selection.selected_trials, start=1)
    }

    for arm in ("baseline", "candidate"):
        source_revision = manifest.arm_source_revisions[arm]
        # Use the production planner so the resume checkpoint has the exact
        # approved identities, arm, repeat, manifest, and corpus seals.
        planner = BenchmarkRunner(manifest, tasks, descriptor, arm=arm, selection=selection)
        planned_keys = planner._planned_keys(source_revision)
        assert len(planned_keys) == selection.expected_trials_by_arm[arm]
        path = tmp_path / f"{arm}.checkpoint.json"
        store = CheckpointStore(
            path,
            header=CheckpointHeader(
                source_revision=source_revision,
                run_manifest_hash=hash_json(manifest.model_dump(mode="json")),
                task_corpus_hash=hash_json([task.model_dump(mode="json") for task in tasks]),
                arm=arm,
            ),
            expected_keys=planned_keys,
            expected_tasks=task_by_id,
            expected_smoke_cells=smoke_cells,
            selection_preflight=selection.checkpoint_preflight(),
            reuse_terminal_trials=True,
        )
        seal = hashlib.sha256(f"synthetic-{arm}-pre-smoke-seal".encode()).hexdigest()
        checkpoint_seals[arm] = seal
        store.bind_pre_smoke_seal(seal, registry)
        for key in planned_keys.values():
            task = task_by_id[key.task_id]
            order_position = planned_arm_order(
                key.task_id,
                key.repeat_index,
                manifest.paired_order_seed,
            ).index(arm)
            sequence = sequence_by_identity[
                (
                    arm,
                    key.model_class,
                    key.transport,
                    key.task_id,
                    key.repeat_index,
                )
            ]
            store.record_paired_event(
                CheckpointPairedEvent(
                    sequence=sequence,
                    cell=f"{key.model_class}:{key.transport}",
                    task_id=key.task_id,
                    repeat_index=key.repeat_index,
                    arm=arm,
                    order_position=order_position,
                    attempt=1,
                )
            )
            store.record_trial(
                key,
                TrialOutcome(
                    task_id=key.task_id,
                    category=task.category,
                    locale=task.locale,
                    arm=arm,
                    model_class=key.model_class,
                    model_id=key.model_id,
                    transport=key.transport,
                    repeat_index=key.repeat_index,
                    input_tokens=20,
                    output_tokens=10,
                    total_tokens=30,
                    model_requests=2,
                    cost_usd=0.01,
                    paired_order_position=order_position,
                    paired_execution_sequence=sequence,
                    error="synthetic terminal provider failure",
                    failure_kind="provider",
                ),
                status="failed",
            )
        store.record_provider_setup_requests(1 if arm == "baseline" else 2)
        checkpoint_spend[arm] = store.document.spent

    class StopAfterRegistration(Exception):
        pass

    class ResumeProbeCoordinator:
        def __init__(self) -> None:
            self.registrations: dict[str, dict[tuple[str, str, int], TrialOutcome]] = {}
            self.history: dict[str, list[dict[str, Any]]] = {}

        async def register_arm(
            self,
            arm: str,
            reused: dict[tuple[str, str, int], TrialOutcome],
            *,
            history_events: list[dict[str, Any]] | None = None,
        ) -> None:
            self.registrations[arm] = reused
            assert history_events is not None
            self.history[arm] = history_events
            raise StopAfterRegistration

        async def wait_until_registered(self) -> None:
            raise AssertionError("resume probe stops after observing reused terminal rows")

    coordinator = ResumeProbeCoordinator()
    ledger = execution_module.BudgetLedger(
        manifest,
        total_cost_limit_usd=selection.budget.new_execution_limit_usd,
    )

    async def fake_preflight(runner: BenchmarkRunner) -> dict[str, Any]:
        return {
            "runtime": {
                "source_revision": manifest.arm_source_revisions[runner.arm],
                "artifact_versions": {
                    "backend_artifact_version": "1.0.0",
                    "proxy_artifact_version": "1.0.0",
                },
                "discovery": {"status": "ready"},
            },
            "resolver": SimpleNamespace(secret_values=lambda: ()),
        }

    monkeypatch.setattr(BenchmarkRunner, "preflight", fake_preflight)

    for arm in ("baseline", "candidate"):
        runner = BenchmarkRunner(
            manifest,
            tasks,
            descriptor,
            arm=arm,
            resume_path=tmp_path / f"{arm}.checkpoint.json",
            paired_coordinator=coordinator,  # type: ignore[arg-type]
            shared_ledger=ledger,
            selection=selection,
        )
        with pytest.raises(StopAfterRegistration):
            await runner.run()

        reused = coordinator.registrations[arm]
        expected = {
            (f"{trial.model_class}:{trial.transport}", trial.task_id, trial.repeat_index)
            for trial in selection.selected_trials
            if trial.arm == arm
        }
        assert set(reused) == expected
        assert all(outcome.arm == arm for outcome in reused.values())
        assert len(coordinator.history[arm]) == len(expected)
        assert runner._checkpoint_store is not None
        assert runner._checkpoint_store.document.header.arm == arm
        assert runner._checkpoint_store.document.header.pre_smoke_seal_hash == checkpoint_seals[arm]
        restored_arms = ("baseline",) if arm == "baseline" else ("baseline", "candidate")
        expected_requests = sum(checkpoint_spend[name].model_requests for name in restored_arms)
        expected_setup_requests = sum(
            checkpoint_spend[name].provider_setup_requests for name in restored_arms
        )
        expected_cost = sum(
            (Decimal(str(checkpoint_spend[name].cost_usd)) for name in restored_arms),
            Decimal("0"),
        )
        assert ledger.requests == expected_requests
        assert ledger.provider_setup_requests == expected_setup_requests
        assert ledger.cost_usd == expected_cost

    assert len(coordinator.registrations["baseline"]) == 13
    assert len(coordinator.registrations["candidate"]) == 10
    assert ledger.requests == 49
    assert ledger.input_tokens == 460
    assert ledger.output_tokens == 230
    assert ledger.provider_setup_requests == 3
    assert ledger.cost_usd == Decimal("0.23")


def test_selection_checkpoint_retains_interrupted_attempt_and_charges_retry_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, tasks, selection = _write_selection(tmp_path / "parents", monkeypatch)
    manifest = selection.execution_manifest
    arm = "baseline"
    task_by_id = {task.id: task for task in tasks}
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    planned_keys = BenchmarkRunner(
        manifest,
        tasks,
        descriptor,
        arm=arm,
        selection=selection,
    )._planned_keys(manifest.arm_source_revisions[arm])
    key = next(iter(planned_keys.values()))
    task = task_by_id[key.task_id]
    smoke_cells = {
        f"{model.class_name}:{transport}": (model.class_name, model.model_id, transport)
        for model in manifest.models
        for transport in manifest.transports
    }
    path = tmp_path / "retry.checkpoint.json"
    header = CheckpointHeader(
        source_revision=manifest.arm_source_revisions[arm],
        run_manifest_hash=hash_json(manifest.model_dump(mode="json")),
        task_corpus_hash=hash_json([task.model_dump(mode="json") for task in tasks]),
        arm=arm,
    )
    store = CheckpointStore(
        path,
        header=header,
        expected_keys=planned_keys,
        expected_tasks=task_by_id,
        expected_smoke_cells=smoke_cells,
        selection_preflight=selection.checkpoint_preflight(),
        reuse_terminal_trials=True,
    )
    store.bind_pre_smoke_seal("a" * 64, {"status": "unavailable", "reason": "synthetic fixture"})
    order_position = planned_arm_order(key.task_id, key.repeat_index, manifest.paired_order_seed).index(arm)
    store.record_paired_event(
        CheckpointPairedEvent(
            sequence=1,
            cell=f"{key.model_class}:{key.transport}",
            task_id=key.task_id,
            repeat_index=key.repeat_index,
            arm=arm,
            order_position=order_position,
        )
    )
    interrupted = TrialOutcome(
        task_id=key.task_id,
        category=task.category,
        locale=task.locale,
        arm=arm,
        model_class=key.model_class,
        model_id=key.model_id,
        transport=key.transport,
        repeat_index=key.repeat_index,
        paired_order_position=order_position,
        paired_execution_sequence=1,
        model_requests=2,
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
        cost_usd=0.01,
        error="synthetic interruption",
        failure_kind="interrupted",
    )
    store.record_trial(key, interrupted, status="failed")
    assert store.reusable_outcome_for(key) is None

    store.record_paired_event(
        CheckpointPairedEvent(
            sequence=2,
            cell=f"{key.model_class}:{key.transport}",
            task_id=key.task_id,
            repeat_index=key.repeat_index,
            arm=arm,
            order_position=order_position,
            attempt=2,
            retry_of_sequence=1,
        )
    )
    retry = interrupted.model_copy(
        update={
            "paired_execution_sequence": 2,
            "model_requests": 3,
            "input_tokens": 30,
            "output_tokens": 15,
            "total_tokens": 45,
            "cost_usd": 0.02,
            "error": "synthetic terminal provider error",
            "failure_kind": "provider",
        }
    )
    store.record_trial(key, retry, status="failed")

    resumed = CheckpointStore(
        path,
        header=header,
        expected_keys=planned_keys,
        expected_tasks=task_by_id,
        expected_smoke_cells=smoke_cells,
        selection_preflight=selection.checkpoint_preflight(),
        resume=True,
        reuse_terminal_trials=True,
    )
    history = resumed.selection_execution_history()
    assert [(event["sequence"], event["attempt"]) for event in history] == [(1, 1), (2, 2)]
    assert history[1]["retry_of_sequence"] == 1
    assert len(resumed.document.attempt_history) == 1
    assert resumed.document.attempt_history[0].outcome == interrupted
    assert resumed.document.spent.model_requests == 5
    assert resumed.document.spent.input_tokens == 50
    assert resumed.document.spent.output_tokens == 25
    assert resumed.document.spent.cost_usd == pytest.approx(0.03)
    assert resumed.reusable_outcome_for(key) == retry


def test_selection_resume_closes_a_start_without_a_checkpointed_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, tasks, selection = _write_selection(tmp_path / "parents", monkeypatch)
    manifest = selection.execution_manifest
    arm = "baseline"
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    planned_keys = BenchmarkRunner(
        manifest,
        tasks,
        descriptor,
        arm=arm,
        selection=selection,
    )._planned_keys(manifest.arm_source_revisions[arm])
    key = next(iter(planned_keys.values()))
    smoke_cells = {
        f"{model.class_name}:{transport}": (model.class_name, model.model_id, transport)
        for model in manifest.models
        for transport in manifest.transports
    }
    header = CheckpointHeader(
        source_revision=manifest.arm_source_revisions[arm],
        run_manifest_hash=hash_json(manifest.model_dump(mode="json")),
        task_corpus_hash=hash_json([task.model_dump(mode="json") for task in tasks]),
        arm=arm,
    )
    path = tmp_path / "abandoned-start.checkpoint.json"
    store = CheckpointStore(
        path,
        header=header,
        expected_keys=planned_keys,
        expected_tasks={task.id: task for task in tasks},
        expected_smoke_cells=smoke_cells,
        selection_preflight=selection.checkpoint_preflight(),
    )
    store.bind_pre_smoke_seal("a" * 64, {"status": "unavailable", "reason": "synthetic fixture"})
    store.record_paired_event(
        CheckpointPairedEvent(
            sequence=1,
            cell=f"{key.model_class}:{key.transport}",
            task_id=key.task_id,
            repeat_index=key.repeat_index,
            arm=arm,
            order_position=planned_arm_order(key.task_id, key.repeat_index, manifest.paired_order_seed).index(arm),
        )
    )
    store.set_run_counters(reused_trials=0, rerun_trials=0)
    store.begin_closing()
    store.finalize_timing({"attempt_index": 1, "wall_seconds": 0, "breakdown": {"other": 0}})

    resumed = CheckpointStore(
        path,
        header=header,
        expected_keys=planned_keys,
        expected_tasks={task.id: task for task in tasks},
        expected_smoke_cells=smoke_cells,
        selection_preflight=selection.checkpoint_preflight(),
        resume=True,
        reuse_terminal_trials=True,
    )

    assert resumed.document.paired_execution_events[0].status == "incomplete"
    assert resumed.document.paired_execution_events[0].record_hash is None
    assert resumed.selection_execution_history() == [
        {
            "sequence": 1,
            "cell": f"{key.model_class}:{key.transport}",
            "task_id": key.task_id,
            "repeat_index": key.repeat_index,
            "arm": arm,
            "order_position": planned_arm_order(
                key.task_id,
                key.repeat_index,
                manifest.paired_order_seed,
            ).index(arm),
            "attempt": 1,
            "status": "incomplete",
        }
    ]


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
            paired_order_position=planned_arm_order(
                trial.task_id,
                trial.repeat_index,
                selection.execution_manifest.paired_order_seed,
            ).index(trial.arm),
            paired_execution_sequence=sequence,
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
                "status": "failed",
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
                "new_trials": selection.expected_trials_by_arm[arm],
                "reused_trials": 0,
                "rerun_trials": 0,
                "spent": {
                    "model_requests": 0,
                    "provider_setup_requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0,
                },
            },
            "paired_execution": {
                "complete": True,
                "events": events,
                "unfinished_starts": [],
                "unknown_start_gaps": [],
            },
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


def test_selection_summary_counts_retry_attempts_and_preserves_unknown_sequence_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest, tasks, selection = _write_selection(tmp_path, monkeypatch)
    artifacts = _complete_diagnostic_artifacts(selection, tasks)
    baseline_trials = [trial for trial in selection.selected_trials if trial.arm == "baseline"]
    candidate_trials = [trial for trial in selection.selected_trials if trial.arm == "candidate"]
    reused = [baseline_trials[0], baseline_trials[1], candidate_trials[0]]
    interrupted = [baseline_trials[2], candidate_trials[1]]

    source_events: list[dict[str, Any]] = []
    latest_sequence: dict[tuple[str, str, str, str, int], int] = {}
    for trial, sequence in zip(reused, (1, 2, 3), strict=True):
        latest_sequence[trial.identity] = sequence
        source_events.append(_selection_event(selection, trial, sequence, status="failed"))
    for trial, sequence in zip(interrupted, (5, 6), strict=True):
        latest_sequence[trial.identity] = sequence
        source_events.append(_selection_event(selection, trial, sequence, status="failed"))

    retry_events = [
        _selection_event(selection, trial, sequence, status="failed", attempt=2, retry_of=prior)
        for trial, sequence, prior in zip(interrupted, (7, 8), (5, 6), strict=True)
    ]
    for trial, event in zip(interrupted, retry_events, strict=True):
        latest_sequence[trial.identity] = event["sequence"]

    pending = [
        trial
        for trial in selection.selected_trials
        if trial.identity not in latest_sequence
    ]
    new_events = [
        _selection_event(selection, trial, sequence, status="failed")
        for sequence, trial in enumerate(pending, start=9)
    ]
    for trial, event in zip(pending, new_events, strict=True):
        latest_sequence[trial.identity] = event["sequence"]

    events = sorted([*source_events, *retry_events, *new_events], key=lambda event: event["sequence"])
    for arm, artifact in artifacts.items():
        for run in artifact["runs"].values():
            for outcome in run["trials"]:
                identity = (
                    arm,
                    outcome["model_class"],
                    outcome["transport"],
                    outcome["task_id"],
                    outcome["repeat_index"],
                )
                outcome["paired_execution_sequence"] = latest_sequence[identity]
        reused_count = 2 if arm == "baseline" else 1
        artifact["checkpoint"].update(
            {
                "new_trials": selection.expected_trials_by_arm[arm] - reused_count,
                "reused_trials": reused_count,
                "rerun_trials": 1,
            }
        )
        artifact["paired_execution"] = {
            "complete": True,
            "events": events,
            "unfinished_starts": [],
            "unknown_start_gaps": [{"sequence": 4, "status": "unknown_start"}],
        }

    summary = selection_module.build_selection_summary(
        selection,
        artifacts,
        {"verdict": "inconclusive"},
        manifest=selection.execution_manifest,
        tasks=tasks,
    )

    assert summary["status"] == "complete"
    assert summary["expected_new_model_trials"] == 20
    assert summary["observed_execution_events"] == 25
    assert summary["observed_unique_selected_trials"] == 23
    assert summary["paired_execution_events_valid"] is True
    assert summary["unknown_start_gaps"] == [{"sequence": 4, "status": "unknown_start"}]


def _selection_event(
    selection: selection_module.ToolGuidanceSelection,
    trial: selection_module.SelectedTrial,
    sequence: int,
    *,
    status: str,
    attempt: int = 1,
    retry_of: int | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "sequence": sequence,
        "cell": f"{trial.model_class}:{trial.transport}",
        "task_id": trial.task_id,
        "repeat_index": trial.repeat_index,
        "arm": trial.arm,
        "order_position": planned_arm_order(
            trial.task_id,
            trial.repeat_index,
            selection.execution_manifest.paired_order_seed,
        ).index(trial.arm),
        "status": status,
    }
    if attempt > 1:
        event["attempt"] = attempt
        event["retry_of_sequence"] = retry_of
    return event


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
