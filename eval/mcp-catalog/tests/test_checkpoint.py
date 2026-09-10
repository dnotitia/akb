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
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        ],
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
