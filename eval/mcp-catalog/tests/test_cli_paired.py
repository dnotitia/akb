from __future__ import annotations

import argparse
import asyncio
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
