from __future__ import annotations

import json
from pathlib import Path

import pytest

import mcp_catalog.cli as cli
from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.execution import BudgetLedger, TrialOutcome
from mcp_catalog.runner import BenchmarkRunFailure, BenchmarkRunner
from mcp_catalog.runtime import RuntimeContractError, RuntimeDescriptor, RuntimeFixture
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


class _Resolver:
    model_secrets = ("fixture-marker",)

    def secret_values(self):
        return self.model_secrets

    def required_profiles(self, _tasks):
        return []

    async def prepare(self, _fixture, _profiles) -> None:
        return None

    async def cleanup(self, _fixture) -> None:
        raise RuntimeContractError("PAT cleanup fixture-marker", stage="pat_cleanup")


@pytest.mark.asyncio
async def test_primary_failure_survives_pat_cleanup_failure_and_serializes_incomplete_artifact(monkeypatch) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    resolver = _Resolver()
    runtime = {
        "source_revision": "a" * 40,
        "artifact_versions": {"backend_artifact_version": "0.0.0", "proxy_artifact_version": "0.0.0"},
        "discovery": {"status": "ready", "scenario": descriptor.scenario},
    }
    primary = RuntimeContractError("fixture readiness failed fixture-marker", stage="fixture_readiness")

    async def fake_preflight(_runner):
        return {"runtime": runtime, "resolver": resolver}

    def fail_build(_spec):
        raise AssertionError("provider model must not be built before runtime readiness")

    monkeypatch.setattr(BenchmarkRunner, "preflight", fake_preflight)
    monkeypatch.setattr("mcp_catalog.runner.build_model", fail_build)

    async def ready(_fixture, *, stage: str) -> None:
        assert stage == "runtime_readiness"
        raise primary

    monkeypatch.setattr(RuntimeFixture, "wait_until_ready", ready)
    closed: list[bool] = []

    async def close(_fixture) -> None:
        closed.append(True)

    monkeypatch.setattr(RuntimeFixture, "close", close)
    runner = BenchmarkRunner(manifest, tasks, descriptor, arm="baseline")

    with pytest.raises(BenchmarkRunFailure) as raised:
        await runner.run()

    failure = raised.value
    assert failure.primary_error is primary
    assert failure.__cause__ is primary
    assert "fixture-marker" not in str(failure)
    assert closed == [True]
    assert failure.artifact["status"] == "incomplete"
    assert failure.artifact["failure"]["stage"] == "fixture_readiness"
    assert failure.artifact["completed_trials"] == 0
    assert failure.artifact["budget_used"]["model_requests"] == 0
    encoded = json.dumps(failure.artifact, ensure_ascii=False)
    assert "fixture-marker" not in encoded
    assert "[redacted]" in encoded
    assert any("cleanup failed" in note and "fixture-marker" not in note for note in primary.__notes__)


@pytest.mark.asyncio
async def test_cli_writes_failure_artifact_before_rethrowing_run_failure(monkeypatch, tmp_path: Path) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    output = tmp_path / "incomplete.json"
    artifact = {
        "schema_version": 1,
        "status": "incomplete",
        "failure": {"stage": "fixture_readiness", "error": "primary secret-value"},
        "completed_trials": 1,
        "budget_used": {"model_requests": 1, "cost_usd": 0.25},
        "partial_payload": "secret-value",
    }
    failure = BenchmarkRunFailure(
        RuntimeContractError("primary secret-value", stage="fixture_readiness"),
        artifact,
        ("secret-value",),
        (),
    )

    class _Runner:
        def __init__(self, *_args, **_kwargs) -> None:
            self.secrets = ()

        async def run(self):
            raise failure

    monkeypatch.setattr(cli, "load_inputs", lambda *_args: (manifest, tasks, descriptor))
    monkeypatch.setattr(cli, "BenchmarkRunner", _Runner)
    args = type(
        "Args",
        (),
        {
            "manifest": ROOT / "config" / "run.json",
            "corpus": ROOT / "corpus" / "tasks.json",
            "descriptor": Path("-"),
            "arm": "baseline",
            "output": output,
        },
    )()

    with pytest.raises(BenchmarkRunFailure):
        await cli.run(args)

    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["status"] == "incomplete"
    assert written["completed_trials"] == 1
    assert written["budget_used"]["model_requests"] == 1
    assert "secret-value" not in output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_incomplete_artifact_keeps_completed_trials_budget_and_redaction() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    runner = BenchmarkRunner(manifest, tasks, descriptor, arm="baseline")
    runner.secrets = ("fixture-marker", "fixture-token")
    outcome = TrialOutcome(
        task_id=tasks[0].id,
        category=tasks[0].category,
        arm="baseline",
        model_class="primary",
        model_id=manifest.models[0].model_id,
        transport="http",
        input_tokens=3,
        output_tokens=2,
        total_tokens=5,
        model_requests=1,
        cost_usd=0.000001,
        provider_evidence=[{"usage": {"api_" + "key": "fixture-marker"}}],
    )
    runner._record_trial("primary:http", outcome)
    ledger = BudgetLedger(manifest)
    await ledger.charge(outcome)
    failure = RuntimeContractError("fixture failed fixture-marker", stage="fixture_readiness")

    artifact = runner._build_artifact(
        runtime={
            "source_revision": "a" * 40,
            "discovery": {"status": "ready", "token": "fixture-token"},
        },
        artifact_versions={"backend_artifact_version": "0.0.0", "proxy_artifact_version": "0.0.0"},
        ledger=ledger,
        catalogs={},
        reports={},
        incomplete_reasons={"benchmark incomplete: fixture failed fixture-marker"},
        failure=failure,
        failure_stage="fixture_readiness",
        cleanup_errors=[RuntimeContractError("PAT cleanup fixture-marker", stage="pat_cleanup")],
    )

    encoded = json.dumps(artifact, ensure_ascii=False)
    assert artifact["status"] == "incomplete"
    assert artifact["completed_trials"] == 1
    assert artifact["budget_used"]["model_requests"] == 1
    assert artifact["budget_used"]["total_tokens"] == 5
    assert artifact["runs"]["primary:http"]["partial"] is True
    assert "fixture-marker" not in encoded
    assert "fixture-token" not in encoded
