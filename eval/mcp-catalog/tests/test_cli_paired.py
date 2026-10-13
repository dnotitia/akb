from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import mcp_catalog.cli as cli_module
from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.runner import BenchmarkRunFailure
from mcp_catalog.runtime import RuntimeContractError, RuntimeDescriptor
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


def _paired_resume_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=cli_module.DEFAULT_MANIFEST,
        corpus=cli_module.DEFAULT_CORPUS,
        coverage=cli_module.DEFAULT_COVERAGE,
        baseline_descriptor=tmp_path / "baseline-descriptor.json",
        candidate_descriptor=tmp_path / "candidate-descriptor.json",
        baseline_output=tmp_path / "baseline.json",
        candidate_output=tmp_path / "candidate.json",
        comparison_output=tmp_path / "comparison.json",
        baseline_checkpoint=tmp_path / "baseline.checkpoint.json",
        candidate_checkpoint=tmp_path / "candidate.checkpoint.json",
        selection=None,
        selection_summary_output=None,
        amendment=None,
        resume=True,
    )


def _parent_file_state(paths: tuple[Path, ...]) -> dict[Path, tuple[bool, str | None, Any]]:
    result = {}
    for path in paths:
        if not path.is_file():
            result[path] = (False, None, None)
            continue
        raw = path.read_bytes()
        lifecycle = json.loads(raw).get("lifecycle") if path.name.endswith("checkpoint.json") else None
        result[path] = (True, hashlib.sha256(raw).hexdigest(), lifecycle)
    return result


def test_run_paired_accepts_the_akb361_amendment_operator() -> None:
    args = cli_module.build_parser().parse_args(
        [
            "run-paired",
            "--baseline-descriptor",
            "baseline-descriptor.json",
            "--candidate-descriptor",
            "candidate-descriptor.json",
            "--baseline-output",
            "baseline.json",
            "--candidate-output",
            "candidate.json",
            "--comparison-output",
            "comparison.json",
            "--baseline-checkpoint",
            "baseline.checkpoint.json",
            "--candidate-checkpoint",
            "candidate.checkpoint.json",
            "--amendment",
            "operator.json",
        ]
    )

    assert args.command == "run-paired"
    assert args.amendment == Path("operator.json")
    assert args.resume is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("present_arm", "expected_error"),
    [
        ("candidate", "missing baseline artifact"),
        ("baseline", "missing candidate artifact"),
        (None, "requires both existing arm artifacts"),
    ],
)
async def test_full_paired_resume_rejects_missing_parent_before_any_resume_activity(
    present_arm: str | None,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _paired_resume_args(tmp_path)
    if present_arm is not None:
        existing_artifact = getattr(args, f"{present_arm}_output")
        existing_artifact.write_text('{"arm":"parent"}', encoding="utf-8")
    for arm in ("baseline", "candidate"):
        checkpoint = getattr(args, f"{arm}_checkpoint")
        checkpoint.write_text('{"lifecycle":"finalized"}', encoding="utf-8")
    parent_paths = (
        args.baseline_output,
        args.candidate_output,
        args.baseline_checkpoint,
        args.candidate_checkpoint,
    )
    before = _parent_file_state(parent_paths)
    unexpected_calls: list[str] = []

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        unexpected_calls.append("called")
        raise AssertionError("missing paired parent must be rejected before resume activity")

    monkeypatch.setattr(cli_module, "load_inputs", unexpected)
    monkeypatch.setattr(cli_module, "_load_paired_resume_inputs", unexpected)
    monkeypatch.setattr(cli_module, "load_paired_resume_source", unexpected)
    monkeypatch.setattr(cli_module, "BenchmarkRunner", unexpected)
    monkeypatch.setattr(
        cli_module.RuntimeDescriptor,
        "from_file",
        classmethod(unexpected),
    )

    with pytest.raises(ValueError, match=expected_error):
        await cli_module.run_paired(args)

    assert unexpected_calls == []
    assert _parent_file_state(parent_paths) == before


@pytest.mark.asyncio
async def test_full_paired_resume_uses_paired_parent_source_path_before_any_run_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _paired_resume_args(tmp_path)
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    parent_summary = {
        "manifest": manifest.model_dump(mode="json"),
        "task_contracts": [task.model_dump(mode="json") for task in tasks],
    }
    for path in (args.baseline_output, args.candidate_output):
        path.write_text(json.dumps(parent_summary), encoding="utf-8")
    for path in (args.baseline_checkpoint, args.candidate_checkpoint):
        path.write_text('{"lifecycle":"finalized"}', encoding="utf-8")
    descriptor = descriptor_dict()
    for path in (args.baseline_descriptor, args.candidate_descriptor):
        path.write_text(json.dumps(descriptor), encoding="utf-8")
    parent_paths = (
        args.baseline_output,
        args.candidate_output,
        args.baseline_checkpoint,
        args.candidate_checkpoint,
    )
    before = _parent_file_state(parent_paths)
    calls: list[str] = []
    native_inputs = cli_module._load_paired_resume_inputs

    def load_paired_inputs(namespace: argparse.Namespace) -> Any:
        calls.append("paired_inputs")
        return native_inputs(namespace)

    class SourceValidationReached(Exception):
        pass

    def load_source(**kwargs: Any) -> Any:
        calls.append("paired_source")
        assert kwargs["artifact_paths"] == {
            "baseline": args.baseline_output,
            "candidate": args.candidate_output,
        }
        assert kwargs["checkpoint_paths"] == {
            "baseline": args.baseline_checkpoint,
            "candidate": args.candidate_checkpoint,
        }
        assert kwargs["manifest"] == manifest
        assert kwargs["tasks"] == tasks
        raise SourceValidationReached

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("full paired resume must not load fresh inputs or start a runner")

    monkeypatch.setattr(cli_module, "_load_paired_resume_inputs", load_paired_inputs)
    monkeypatch.setattr(cli_module, "load_paired_resume_source", load_source)
    monkeypatch.setattr(cli_module, "load_inputs", unexpected)
    monkeypatch.setattr(cli_module, "BenchmarkRunner", unexpected)

    with pytest.raises(SourceValidationReached):
        await cli_module.run_paired(args)

    assert calls == ["paired_inputs", "paired_source"]
    assert _parent_file_state(parent_paths) == before


@pytest.mark.asyncio
async def test_paired_first_exception_writes_artifact_for_cancelled_preflight_peer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    candidate_started = asyncio.Event()

    class Runner:
        def __init__(self, *_args: object, **kwargs: Any) -> None:
            self.arm = kwargs["arm"]
            self.secrets: tuple[str, ...] = ()

        async def run(self) -> dict[str, object]:
            if self.arm == "baseline":
                await candidate_started.wait()
                raise BenchmarkRunFailure(
                    RuntimeContractError("smoke failure", stage="smoke_gate"),
                    {"schema_version": 2, "arm": "baseline", "status": "incomplete"},
                    (),
                    (),
                )

            candidate_started.set()
            await asyncio.Event().wait()
            return {}

    monkeypatch.setattr(cli_module, "load_inputs", lambda *_args: (manifest, tasks, descriptor))
    monkeypatch.setattr(cli_module, "BenchmarkRunner", Runner)
    args = argparse.Namespace(
        manifest=Path("manifest.json"),
        corpus=Path("tasks.json"),
        coverage=Path("coverage.json"),
        baseline_descriptor=Path("baseline.json"),
        candidate_descriptor=Path("candidate.json"),
        baseline_output=tmp_path / "baseline.json",
        candidate_output=tmp_path / "candidate.json",
        comparison_output=tmp_path / "comparison.json",
        baseline_checkpoint=tmp_path / "baseline.checkpoint.json",
        candidate_checkpoint=tmp_path / "candidate.checkpoint.json",
        resume=False,
    )

    result = await cli_module.run_paired(args)

    baseline = json.loads(args.baseline_output.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate_output.read_text(encoding="utf-8"))
    comparison = json.loads(args.comparison_output.read_text(encoding="utf-8"))
    assert result == 1
    assert baseline["arm"] == "baseline"
    assert candidate["arm"] == "candidate"
    assert candidate["failure"]["stage"] == "paired_execution"
    assert candidate["failure"]["type"] == "CancelledError"
    assert candidate["checkpoint_path"] == str(args.candidate_checkpoint)
    assert candidate["paired_execution"]["complete"] is False
    assert candidate["paired_budget_used"]["model_requests"] == 0
    assert candidate["paired_budget_used"]["cost_usd"] == 0
    assert comparison["verdict"] == "inconclusive"
