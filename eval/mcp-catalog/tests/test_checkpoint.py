from __future__ import annotations

import json
import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from mcp_catalog.checkpoint import (
    CheckpointError,
    CheckpointHeader,
    CheckpointKey,
    CheckpointStore,
    checkpoint_record_hash,
    provider_registry_snapshot_hash,
)
from mcp_catalog.contracts import hash_json, load_run_manifest, load_task_corpus
from mcp_catalog.execution import BudgetExceeded, BudgetLedger, ToolCallRecord, TrialOutcome
from paired_artifact_factory import (
    SYNTHETIC_SERVER_INSTRUCTIONS,
    provider_name_for_model,
    synthetic_model_input_evidence,
)

ROOT = Path(__file__).parents[1]
_TEST_PROVIDER_REGISTRY = {"status": "unavailable", "reason": "synthetic checkpoint fixture"}
_TEST_SELECTION_PREFLIGHT = {
    "read_only": True,
    "selection_path": "/tmp/selection.json",
    "selection_sha256": "a" * 64,
    "authorization": {},
    "parents": {},
    "parent_count": 0,
    "selected_trial_count": 0,
    "role_counts": {},
    "budget": {},
}


def _inputs() -> tuple[object, list[object], CheckpointHeader, CheckpointKey, dict[str, CheckpointKey]]:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    source_revision = "a" * 40
    header = CheckpointHeader(
        source_revision=source_revision,
        run_manifest_hash=hash_json(manifest.model_dump(mode="json")),
        task_corpus_hash=hash_json([task.model_dump(mode="json") for task in tasks]),
        arm="baseline",
    )
    task = tasks[0]
    key = CheckpointKey(
        source_revision=header.source_revision,
        run_manifest_hash=header.run_manifest_hash,
        task_corpus_hash=header.task_corpus_hash,
        arm=header.arm,
        model_class="primary",
        model_id=manifest.models[0].model_id,
        transport="http",
        task_id=task.id,
        repeat_index=1,
        locale=task.locale,
    )
    return manifest, tasks, header, key, {hash_json(key.model_dump(mode="json")): key}


def _outcome(key: CheckpointKey, *, error: str | None = None) -> TrialOutcome:
    task = next(task for task in _inputs()[1] if task.id == key.task_id)
    manifest = _inputs()[0]
    model_spec = next(model for model in manifest.models if model.model_id == key.model_id)
    return TrialOutcome(
        task_id=key.task_id,
        category="single_operation",
        arm=key.arm,
        model_class=key.model_class,
        model_id=key.model_id,
        transport=key.transport,
        locale=key.locale,
        repeat_index=key.repeat_index,
        tool_calls=[],
        final_answer_text="완료",
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        model_requests=1,
        cost_usd=0.00001,
        provider_evidence=[
            {
                "model": key.model_id,
                "routing": {
                    "endpoints": {
                        "available": [{"provider": provider_name_for_model(model_spec), "selected": True}]
                    }
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001},
            }
        ],
        provider_cost_usd=0.00001,
        cost_source="provider_response",
        routing_observed=True,
        routing_valid=True,
        server_instructions_sha256=hashlib.sha256(SYNTHETIC_SERVER_INSTRUCTIONS.encode("utf-8")).hexdigest(),
        model_input_evidence=synthetic_model_input_evidence(1),
        state_available_before=True,
        state_available_after=True,
        state_before={},
        state_after={},
        state_observations_before=[
            {
                "available": True,
                "status_code": item.resolved_before_expected_status,
                "payload": {},
                "error": None,
            }
            for item in task.expected_final_state.observation_sets
        ],
        state_observations_after=[
            {
                "available": True,
                "status_code": item.probe.expected_status,
                "payload": {},
                "error": None,
            }
            for item in task.expected_final_state.observation_sets
        ],
        error=error,
    )


def _store(
    path: Path,
    *,
    resume: bool = False,
    secrets: tuple[str, ...] = (),
    reuse_terminal_trials: bool = False,
) -> tuple[CheckpointStore, CheckpointKey]:
    _manifest, tasks, header, key, expected = _inputs()
    store = CheckpointStore(
            path,
            header=header,
            expected_keys=expected,
            expected_tasks={task.id: task for task in tasks},
            expected_smoke_cells={
                "primary:http": ("primary", key.model_id, "http"),
                "primary:stdio": ("primary", key.model_id, "stdio"),
                "lightweight:http": ("lightweight", "qwen/qwen3.8-27b", "http"),
                "lightweight:stdio": ("lightweight", "qwen/qwen3.8-27b", "stdio"),
            },
            secrets=secrets,
            selection_preflight=_TEST_SELECTION_PREFLIGHT,
            resume=resume,
            reuse_terminal_trials=reuse_terminal_trials,
        )
    if not resume:
        store.bind_pre_smoke_seal("e" * 64, _TEST_PROVIDER_REGISTRY)
    return store, key


def test_trial_checkpoint_is_atomic_redacted_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "run" / "checkpoint.json"
    store, key = _store(path, secrets=("provider-secret",))
    store.record_trial(key, _outcome(key), status="completed")

    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text(encoding="utf-8")
    assert "provider-secret" not in raw
    resumed, _ = _store(path, resume=True, secrets=("provider-secret",))
    reused = resumed.completed_outcome_for(key)
    assert reused is not None
    assert resumed.document.spent.model_requests == 1


def test_checkpoint_pins_selection_and_exact_provider_registry_snapshots(tmp_path: Path) -> None:
    path = tmp_path / "pinned-inputs.checkpoint.json"
    store, _key = _store(path)
    assert store.document.provider_registry_snapshot == _TEST_PROVIDER_REGISTRY
    assert store.document.header.provider_registry_hash == provider_registry_snapshot_hash(
        _TEST_PROVIDER_REGISTRY
    )
    assert store.document.selection_preflight == _TEST_SELECTION_PREFLIGHT

    revalidation = {"status": "unavailable", "reason": "synthetic fresh verification"}
    store.record_provider_registry_revalidation(revalidation, invariant_sha256=None)
    resumed, _key = _store(path, resume=True)
    assert resumed.document.provider_registry_snapshot == _TEST_PROVIDER_REGISTRY
    assert resumed.document.provider_registry_revalidation[0]["snapshot"] == revalidation

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["provider_registry_revalidation"][0]["snapshot"]["reason"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointError, match="revalidation hash"):
        _store(path, resume=True)

    raw["provider_registry_revalidation"][0]["snapshot"] = revalidation
    raw["provider_registry_revalidation"][0]["snapshot_hash"] = hash_json(revalidation)
    raw["provider_registry_snapshot"]["reason"] = "tampered frozen registry"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointError, match="provider registry hash"):
        _store(path, resume=True)

    raw["provider_registry_snapshot"] = _TEST_PROVIDER_REGISTRY
    raw["selection_preflight"]["selection_sha256"] = "b" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointError, match="selection preflight"):
        _store(path, resume=True)


def test_checkpoint_rejects_model_input_hash_drift_on_reload(tmp_path: Path) -> None:
    path = tmp_path / "model-input.checkpoint.json"
    store, key = _store(path)
    store.record_trial(key, _outcome(key), status="completed")
    raw = json.loads(path.read_text(encoding="utf-8"))
    digest = hash_json(key.model_dump(mode="json"))
    record = raw["records"][digest]
    record["outcome"]["model_input_evidence"][0]["model_input_canonical_json"] = (
        '{"instructions":"Synthetic server instructions.","request_index":1,"tampered":true}'
    )
    outcome = TrialOutcome.model_validate(record["outcome"])
    record["record_hash"] = checkpoint_record_hash(record["status"], key, outcome)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CheckpointError, match="model input evidence"):
        _store(path, resume=True)


def test_checkpoint_rejects_redaction_that_would_break_model_input_hash(tmp_path: Path) -> None:
    marker = "synthetic-user"
    path = tmp_path / "redacted-input.checkpoint.json"
    store, key = _store(path, secrets=(marker,))
    outcome = _outcome(key)
    evidence = outcome.model_input_evidence[0]
    serialized = '{"instructions":"Synthetic server instructions.","username":"synthetic-user"}'
    redaction_sensitive = evidence.model_copy(
        update={
            "model_input_canonical_json": serialized,
            "model_input_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        }
    )
    outcome = outcome.model_copy(update={"model_input_evidence": [redaction_sensitive]})

    with pytest.raises(CheckpointError, match="model input evidence"):
        store.record_trial(key, outcome, status="completed")

    assert json.loads(path.read_text(encoding="utf-8"))["records"] == {}


def test_selection_resume_reuses_failed_and_incomplete_terminal_rows(tmp_path: Path) -> None:
    for status in ("failed", "incomplete"):
        path = tmp_path / f"{status}.json"
        store, key = _store(path, reuse_terminal_trials=True)
        outcome = _outcome(key, error="synthetic first outcome")
        store.record_trial(key, outcome, status=status)

        assert store.reusable_outcome_for(key) == outcome


def test_selection_resume_rejects_completed_rows_without_instruction_proof(tmp_path: Path) -> None:
    store, key = _store(tmp_path / "completed.json", reuse_terminal_trials=True)
    outcome = _outcome(key).model_copy(
        update={"server_instructions_sha256": None, "model_input_evidence": []}
    )
    store.record_trial(key, outcome, status="completed")

    with pytest.raises(CheckpointError, match="invalid completed trial"):
        store.reusable_outcome_for(key)


def test_resume_rejects_checkpoint_from_a_prior_task_corpus_seal(tmp_path: Path) -> None:
    manifest, tasks, current_header, current_key, current_expected = _inputs()
    old_hash = hash_json({"prior_task_corpus": current_header.task_corpus_hash})
    old_header = current_header.model_copy(update={"task_corpus_hash": old_hash})
    old_key = current_key.model_copy(update={"task_corpus_hash": old_hash})
    old_expected = {hash_json(old_key.model_dump(mode="json")): old_key}
    path = tmp_path / "prior-seal.checkpoint.json"
    smoke_cells = {
        "primary:http": ("primary", manifest.models[0].model_id, "http"),
        "primary:stdio": ("primary", manifest.models[0].model_id, "stdio"),
        "lightweight:http": ("lightweight", "qwen/qwen3.8-27b", "http"),
        "lightweight:stdio": ("lightweight", "qwen/qwen3.8-27b", "stdio"),
    }
    old_store = CheckpointStore(
        path,
        header=old_header,
        expected_keys=old_expected,
        expected_tasks={task.id: task for task in tasks},
        expected_smoke_cells=smoke_cells,
    )
    old_store.bind_pre_smoke_seal("e" * 64, _TEST_PROVIDER_REGISTRY)
    old_store.record_trial(old_key, _outcome(old_key), status="completed")

    with pytest.raises(CheckpointError):
        CheckpointStore(
            path,
            header=current_header,
            expected_keys=current_expected,
            expected_tasks={task.id: task for task in tasks},
            expected_smoke_cells=smoke_cells,
            resume=True,
        )


@pytest.mark.parametrize("record_as_smoke", [False, True])
@pytest.mark.asyncio
async def test_checkpoint_restores_terminal_budget_failure(
    tmp_path: Path,
    record_as_smoke: bool,
) -> None:
    path = tmp_path / ("smoke.json" if record_as_smoke else "trial.json")
    store, key = _store(path)
    outcome = _outcome(key, error="benchmark incomplete: max_total_cost_usd exceeded").model_copy(
        update={"failure_kind": "budget"}
    )
    if record_as_smoke:
        store.record_smoke_cell("primary:http", outcome, status="failed")
    else:
        store.record_trial(key, outcome, status="failed")

    resumed, _ = _store(path, resume=True)

    budget_failure = resumed.budget_failure_reason()
    assert budget_failure == "benchmark incomplete: max_total_cost_usd exceeded"
    manifest, _tasks, _header, _key, _expected = _inputs()
    ledger = BudgetLedger(manifest)
    ledger.restore(
        model_requests=resumed.document.spent.model_requests,
        input_tokens=resumed.document.spent.input_tokens,
        output_tokens=resumed.document.spent.output_tokens,
        cost_usd=resumed.document.spent.cost_usd,
        wall_seconds=resumed.document.spent.wall_seconds,
        model_work_seconds=resumed.document.spent.model_work_seconds,
        budget_failure=budget_failure,
    )
    with pytest.raises(BudgetExceeded, match="max_total_cost_usd"):
        await ledger.reserve_trial(manifest.budget.max_cost_per_trial_usd)


@pytest.mark.asyncio
async def test_checkpoint_does_not_promote_per_trial_failure_to_global_stop(tmp_path: Path) -> None:
    path = tmp_path / "per-trial-budget.json"
    store, key = _store(path)
    store.record_trial(
        key,
        _outcome(key, error="benchmark incomplete: max_cost_per_trial_usd exceeded").model_copy(
            update={"failure_kind": "budget"}
        ),
        status="failed",
    )
    resumed, _ = _store(path, resume=True)
    assert resumed.budget_failure_reason() is None

    manifest, _tasks, _header, _key, _expected = _inputs()
    ledger = BudgetLedger(manifest)
    ledger.restore(
        model_requests=resumed.document.spent.model_requests,
        input_tokens=resumed.document.spent.input_tokens,
        output_tokens=resumed.document.spent.output_tokens,
        cost_usd=resumed.document.spent.cost_usd,
        wall_seconds=resumed.document.spent.wall_seconds,
        model_work_seconds=resumed.document.spent.model_work_seconds,
        budget_failure=resumed.budget_failure_reason(),
    )
    reservation = await ledger.reserve_trial(Decimal(str(manifest.budget.max_cost_per_trial_usd)))
    await reservation.release()


def test_resume_fails_closed_for_header_mismatch_corruption_and_secret(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    store.record_trial(key, _outcome(key), status="completed")

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["header"]["arm"] = "candidate"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointError, match="exact-input header"):
        _store(path, resume=True)

    raw["header"]["arm"] = "baseline"
    raw["records"][next(iter(raw["records"]))]["key"]["repeat_index"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointError):
        _store(path, resume=True)

    raw["records"][next(iter(raw["records"]))]["key"]["repeat_index"] = 1
    raw["records"][next(iter(raw["records"]))]["outcome"]["final_answer_text"] = "provider-secret"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointError, match="secret"):
        _store(path, resume=True, secrets=("provider-secret",))

    path.unlink()
    store, key = _store(path)
    store.record_trial(key, _outcome(key), status="completed")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["spent"]["model_requests"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointError, match="spent-usage digest"):
        _store(path, resume=True)


def test_failed_checkpoint_trial_is_reused_and_spent_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    failed = _outcome(key, error="provider failed")
    store.record_trial(key, failed, status="failed")

    resumed, _ = _store(path, resume=True)
    assert resumed.completed_outcome_for(key) is None
    assert resumed.reusable_outcome_for(key) == failed
    assert resumed.status_for(key) == "failed"
    assert resumed.document.spent.model_requests == 1


def test_replacing_settled_checkpoint_row_does_not_double_spent_usage(tmp_path: Path) -> None:
    store, key = _store(tmp_path / "checkpoint.json")
    failed = _outcome(key, error="provider failed")
    completed = _outcome(key)

    assert store.record_trial(key, failed, status="failed") is False
    assert store.document.spent.model_requests == 1
    assert store.record_trial(key, completed, status="completed") is True
    assert store.document.spent.model_requests == 1
    assert store.document.spent.input_tokens == completed.input_tokens
    assert store.document.spent.output_tokens == completed.output_tokens


def test_explicit_continuation_reuses_failed_parent_outcomes_without_retry(tmp_path: Path) -> None:
    path = tmp_path / "continuation.checkpoint.json"
    lineage = {"schema_version": 1, "work_item": "AKB-361"}
    manifest, tasks, header, key, expected = _inputs()
    store = CheckpointStore(
        path,
        header=header,
        expected_keys=expected,
        expected_tasks={task.id: task for task in tasks},
        expected_smoke_cells={
            "primary:http": ("primary", key.model_id, "http"),
            "primary:stdio": ("primary", key.model_id, "stdio"),
            "lightweight:http": ("lightweight", "qwen/qwen3.8-27b", "http"),
            "lightweight:stdio": ("lightweight", "qwen/qwen3.8-27b", "stdio"),
        },
        continuation=lineage,
    )
    store.bind_pre_smoke_seal("e" * 64, _TEST_PROVIDER_REGISTRY)
    failed = _outcome(key, error="recorded parent failure")
    store.record_trial(key, failed, status="failed")

    resumed = CheckpointStore(
        path,
        header=header,
        expected_keys=expected,
        expected_tasks={task.id: task for task in tasks},
        expected_smoke_cells={
            "primary:http": ("primary", key.model_id, "http"),
            "primary:stdio": ("primary", key.model_id, "stdio"),
            "lightweight:http": ("lightweight", "qwen/qwen3.8-27b", "http"),
            "lightweight:stdio": ("lightweight", "qwen/qwen3.8-27b", "stdio"),
        },
        continuation=lineage,
        resume=True,
    )

    assert resumed.completed_outcome_for(key) is None
    assert resumed.reusable_outcome_for(key) == failed
    assert resumed.status_for(key) == "failed"
    assert resumed.document.continuation == lineage
    assert manifest.budget.max_cost_per_trial_usd > 0


def test_retired_request_limit_failure_kind_is_not_a_trial_compatibility_value() -> None:
    raw = _outcome(_inputs()[3], error="benchmark incomplete: max_model_requests exceeded").model_dump(mode="json")
    raw["failure_kind"] = "request_limit"

    with pytest.raises(ValueError, match="failure_kind"):
        TrialOutcome.model_validate(raw)


def test_checkpoint_outcome_without_call_trace_is_not_reusable(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    store.record_trial(key, _outcome(key), status="completed")
    raw = json.loads(path.read_text(encoding="utf-8"))
    next(iter(raw["records"].values()))["outcome"].pop("tool_calls")
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CheckpointError, match="invalid completed trial"):
        _store(path, resume=True)


def test_measured_behavioral_failure_is_completed_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    outcome = _outcome(key, error="Model token limit (8192) exceeded").model_copy(
        update={
            "failure_kind": "output_limit",
            "state_available_before": True,
            "state_available_after": True,
        }
    )

    store.record_trial(key, outcome)

    resumed, _ = _store(path, resume=True)
    assert resumed.status_for(key) == "completed"
    assert resumed.completed_outcome_for(key) is not None
    assert resumed.document.spent.cost_usd == pytest.approx(0.00001)


def test_provider_failure_without_usage_is_not_reusable(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    outcome = _outcome(key, error="ModelHTTPError: status=429").model_copy(
        update={
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "model_requests": 0,
            "cost_usd": 0.0,
            "provider_evidence": [],
            "provider_cost_usd": None,
            "cost_source": "registered_price_snapshot",
            "routing_observed": False,
            "routing_valid": False,
            "failure_kind": "provider",
        }
    )

    store.record_trial(key, outcome)

    resumed, _ = _store(path, resume=True)
    assert resumed.status_for(key) == "failed"
    assert resumed.completed_outcome_for(key) is None


def test_checkpoint_timing_is_cumulative_across_resume_attempts(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    timing = {
        "attempt_index": 1,
        "wall_seconds": 12.0,
        "breakdown": {"model_execution": 9.0, "fixture_reset": 2.0, "other": 1.0},
    }
    store.record_trial(key, _outcome(key), status="completed", timing=timing)
    store.finalize_timing(timing)

    resumed, _ = _store(path, resume=True)
    assert resumed.next_timing_attempt_index() == 2
    assert resumed.document.timing.cumulative_wall_seconds == pytest.approx(12.0)
    assert len(resumed.document.timing.attempts) == 1
    assert resumed.document.timing.active_attempt is None


def test_checkpoint_timing_tampering_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    store.record_trial(key, _outcome(key), status="completed", timing={
        "attempt_index": 1,
        "wall_seconds": 2.0,
        "breakdown": {"other": 2.0},
    })
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["timing"]["cumulative_wall_seconds"] = 3.0
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CheckpointError, match="timing digest"):
        _store(path, resume=True)


def test_checkpoint_closing_rejects_late_writes_and_finalizes_once(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    timing = {
        "attempt_index": 1,
        "wall_seconds": 3.0,
        "breakdown": {"model_execution": 3.0},
    }

    store.begin_closing()
    with pytest.raises(CheckpointError, match="late writes"):
        store.record_trial(key, _outcome(key), status="failed")
    with pytest.raises(CheckpointError, match="late writes"):
        store.update_timing(timing)

    store.finalize_timing(timing)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["lifecycle"] == "finalized"
    assert raw["reserved_cost_usd"] == 0
    assert raw["timing"]["active_attempt"] is None

    with pytest.raises(CheckpointError, match="finalized"):
        store.finalize_timing(timing)


def test_resume_rejects_closing_or_orphaned_checkpoint_state(tmp_path: Path) -> None:
    closing_path = tmp_path / "closing.json"
    closing, _ = _store(closing_path)
    closing.begin_closing()
    with pytest.raises(CheckpointError, match="finalization"):
        _store(closing_path, resume=True)

    reserved_path = tmp_path / "reserved.json"
    reserved, _ = _store(reserved_path)
    reserved.set_reserved_cost(0.1)
    with pytest.raises(CheckpointError, match="orphaned reserved cost"):
        _store(reserved_path, resume=True)


def test_smoke_checkpoint_requires_a_successful_call_and_follow_up_response(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    smoke_cells = {
        "primary:http": ("primary", key.model_id, "http"),
        "primary:stdio": ("primary", key.model_id, "stdio"),
        "lightweight:http": ("lightweight", "qwen/qwen3.8-27b", "http"),
        "lightweight:stdio": ("lightweight", "qwen/qwen3.8-27b", "stdio"),
    }
    for cell, (model_class, model_id, transport) in smoke_cells.items():
        smoke = _outcome(key).model_copy(
            update={
                "model_class": model_class,
                "model_id": model_id,
                "transport": transport,
                "tool_calls": [
                    ToolCallRecord(
                        order=1,
                        tool_name="find_documents",
                        logical_operation="search",
                        transport_succeeded=True,
                        server_succeeded=True,
                    )
                    ],
                    "successful_mcp_tool_calls": 1,
                    "first_logical_operation": "search",
                    "follow_up_terminal_response": True,
                "model_requests": 2,
                    "model_input_evidence": synthetic_model_input_evidence(2),
                "provider_evidence": [
                    {
                        "model": model_id,
                        "routing": {
                            "endpoints": {
                                "available": [{
                                    "provider": provider_name_for_model(
                                        next(model for model in _inputs()[0].models if model.model_id == model_id)
                                    ),
                                    "selected": True,
                                }]
                            }
                        },
                        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.000005},
                    }
                    for _ in range(2)
                ],
                "provider_cost_usd": 0.00001,
            }
        )
        store.record_smoke_cell(cell, smoke, status="completed")
    store.set_smoke_status("passed")

    resumed, _ = _store(path, resume=True)
    assert resumed.smoke_is_complete()
    assert resumed.smoke_outcome_for("primary:http") is not None
