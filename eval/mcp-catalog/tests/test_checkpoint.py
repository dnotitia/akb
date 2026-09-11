from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_catalog.checkpoint import (
    CheckpointError,
    CheckpointHeader,
    CheckpointKey,
    CheckpointStore,
)
from mcp_catalog.contracts import hash_json, load_run_manifest, load_task_corpus
from mcp_catalog.execution import TrialOutcome

ROOT = Path(__file__).parents[1]


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
    return TrialOutcome(
        task_id=key.task_id,
        category="single_operation",
        arm=key.arm,
        model_class=key.model_class,
        model_id=key.model_id,
        transport=key.transport,
        locale=key.locale,
        repeat_index=key.repeat_index,
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
                        "available": [{"provider": "parasail", "selected": True}]
                    }
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001},
            }
        ],
        provider_cost_usd=0.00001,
        cost_source="provider_response",
        routing_observed=True,
        routing_valid=True,
        error=error,
    )


def _store(path: Path, *, resume: bool = False, secrets: tuple[str, ...] = ()) -> tuple[CheckpointStore, CheckpointKey]:
    _manifest, _tasks, header, key, expected = _inputs()
    return (
        CheckpointStore(
            path,
            header=header,
            expected_keys=expected,
            expected_smoke_cells={
                "primary:http": ("primary", key.model_id, "http"),
                "primary:stdio": ("primary", key.model_id, "stdio"),
                "lightweight:http": ("lightweight", "qwen/qwen3.8-27b", "http"),
                "lightweight:stdio": ("lightweight", "qwen/qwen3.8-27b", "stdio"),
            },
            secrets=secrets,
            resume=resume,
        ),
        key,
    )


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


def test_failed_checkpoint_trial_is_not_reused_but_spent_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    store, key = _store(path)
    store.record_trial(key, _outcome(key, error="provider failed"), status="failed")

    resumed, _ = _store(path, resume=True)
    assert resumed.completed_outcome_for(key) is None
    assert resumed.status_for(key) == "failed"
    assert resumed.document.spent.model_requests == 1


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
                "successful_mcp_tool_calls": 1,
                "follow_up_terminal_response": True,
                "model_requests": 2,
            }
        )
        store.record_smoke_cell(cell, smoke, status="completed")
    store.set_smoke_status("passed")

    resumed, _ = _store(path, resume=True)
    assert resumed.smoke_is_complete()
    assert resumed.smoke_outcome_for("primary:http") is not None
