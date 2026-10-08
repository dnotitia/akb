from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_catalog.runner as runner_module
from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.execution import ToolCallRecord, TrialOutcome
from mcp_catalog.runner import BenchmarkRunner
from mcp_catalog.runtime import RuntimeDescriptor
from paired_artifact_factory import _catalog_snapshot, provider_name_for_model, provider_registry_snapshot
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


class _ParallelResolver:
    def required_profiles(self, _tasks):
        return sorted({task.fixture.credential_profile for task in _tasks})

    async def prepare(self, _fixture, _profiles) -> None:
        return None

    def token_for(self, _profile: str) -> str:
        return "fixture-token"

    def secrets_for(self, _profile: str) -> tuple[str, ...]:
        return ("fixture-token",)

    def secret_values(self) -> tuple[str, ...]:
        return ("fixture-token",)

    async def refresh_after_reset(self, _fixture, _profile: str) -> str:
        return f"{_fixture.descriptor.app_origin}:fresh-token"

    def mark_reset_complete(self) -> None:
        return None

    async def cleanup(self, _fixture) -> None:
        return None


class _ParallelFixture:
    active = 0
    maximum_active = 0
    instances: list[_ParallelFixture] = []

    def __init__(self, descriptor) -> None:
        self.descriptor = descriptor
        self.reset_calls = 0
        self.__class__.instances.append(self)

    async def wait_until_ready(self, *, stage: str) -> None:
        return None

    async def reset(self) -> None:
        self.reset_calls += 1

    async def close(self) -> None:
        return None

    def reset_evidence(self) -> dict[str, float | int]:
        return {"count": self.reset_calls, "wall_seconds": 0.0}


class _ParallelReport:
    def __init__(self, outcomes: list[TrialOutcome]) -> None:
        self.failures: list[object] = []
        self.cases = [SimpleNamespace(output=outcome) for outcome in outcomes]


def _parallel_descriptor() -> RuntimeDescriptor:
    raw = descriptor_dict()
    raw["scenario"] = "app-control-plane"
    raw["services"]["fixture"]["reset"]["body"] = {"scenario": "app-control-plane"}
    descriptor = RuntimeDescriptor.from_dict(raw)
    keys = ("primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio")
    cells = {}
    for index, key in enumerate(keys):
        app_port = 8000 + index * 10
        fixture_port = 8889 + index * 10
        cells[key] = dataclasses.replace(
            descriptor,
            app_origin=f"http://127.0.0.1:{app_port}",
            app_health_url=f"http://127.0.0.1:{app_port}/readyz",
            fixture_origin=f"http://127.0.0.1:{fixture_port}",
            fixture_health_url=f"http://127.0.0.1:{fixture_port}/health",
            reset_url=f"http://127.0.0.1:{fixture_port}/reset",
            discovery_url=f"http://127.0.0.1:{fixture_port}/discover",
        )
    return dataclasses.replace(descriptor, benchmark_cells=cells)


def _empty_state_payload(task) -> dict | list[dict]:
    payloads = [{} for _item in task.expected_final_state.observation_sets]
    return payloads[0] if len(payloads) == 1 else payloads


def _outcome(task, model_spec, transport, repeat_index: int) -> TrialOutcome:
    return TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class=model_spec.class_name,
        model_id=model_spec.model_id,
        transport=transport,
        repeat_index=repeat_index,
        final_answer_text="완료",
        tool_calls=[
            ToolCallRecord(
                order=1,
                tool_name="fixture_tool",
                logical_operation="search",
                transport_succeeded=True,
                server_succeeded=True,
            )
        ],
        successful_mcp_tool_calls=1,
        first_logical_operation="search",
        follow_up_terminal_response=True,
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        model_requests=2,
        cost_usd=0.00001,
        provider_evidence=[
            {
                "model": model_spec.model_id,
                "routing": {
                    "requested": model_spec.model_id,
                    "endpoints": {
                        "available": [{
                            "model": runner_module._expected_openrouter_canonical_slug(model_spec),
                            "provider": provider_name_for_model(model_spec),
                            "selected": True,
                        }]
                    },
                },
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.000005},
            }
            for _ in range(2)
        ],
        provider_cost_usd=0.00001,
        cost_source="provider_response",
        routing_observed=True,
        routing_valid=True,
        state_available_before=True,
        state_available_after=True,
        state_before=_empty_state_payload(task),
        state_after=_empty_state_payload(task),
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
    )


@pytest.mark.asyncio
async def test_registered_cells_run_in_parallel_and_keep_deterministic_hash_input(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    descriptor = _parallel_descriptor()
    resolver = _ParallelResolver()
    active = 0
    maximum_active = 0

    async def fake_preflight(_runner):
        return {
            "runtime": {
                "source_revision": manifest.arm_source_revisions["baseline"],
                "artifact_versions": {
                    "backend_artifact_version": "0.0.0",
                    "proxy_artifact_version": "0.0.0",
                },
                "discovery": {"status": "ready"},
            },
            "resolver": resolver,
        }

    async def fake_capture(*_args, **kwargs):
        assert kwargs["token"] == f"{_args[0].descriptor.app_origin}:fresh-token"
        return _catalog_snapshot(manifest, "baseline", kwargs["transport"]).model_copy(
            update={"source_revision": kwargs["source_revision"], "artifact_version": kwargs["artifact_version"]}
        )

    async def fake_smoke(task, *, model_spec, transport, **_kwargs):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _outcome(task, model_spec, transport, 1)

    async def fake_evaluate(tasks_for_repeat, *, executor, repeat_indices, **_kwargs):
        nonlocal active, maximum_active
        for _task in tasks_for_repeat:
            await _kwargs["fixture"].reset()
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _ParallelReport(
            [_outcome(task, executor.model_spec, executor.transport, repeat_indices[task.id]) for task in tasks_for_repeat]
        )

    monkeypatch.setattr(runner_module, "RuntimeFixture", _ParallelFixture)
    monkeypatch.setattr(BenchmarkRunner, "preflight", fake_preflight)
    async def fake_provider_registry(self, _ledger):
        self.provider_registry = provider_registry_snapshot(manifest)
        return self.provider_registry

    monkeypatch.setattr(BenchmarkRunner, "_capture_provider_registry", fake_provider_registry)
    monkeypatch.setattr(runner_module, "capture_catalog", fake_capture)
    monkeypatch.setattr(runner_module, "build_model", lambda spec: SimpleNamespace(settings={}, model_spec=spec))
    monkeypatch.setattr(runner_module, "execute_smoke", fake_smoke)
    monkeypatch.setattr(runner_module, "evaluate_dataset", fake_evaluate)
    monkeypatch.setattr(runner_module, "serialize_report", lambda *_args, **_kwargs: {})

    parallel = await BenchmarkRunner(manifest, tasks, descriptor, arm="baseline").run()
    parallel_fixtures = list(_ParallelFixture.instances)
    second_parallel = await BenchmarkRunner(manifest, tasks, descriptor, arm="baseline").run()

    expected_trials = sum(
        1
        for model in manifest.models
        for transport in manifest.transports
        for task in tasks
        if transport in task.fixture.transports
        for _repeat in range(1, manifest.repeats + 1)
    )
    assert maximum_active == 4
    assert parallel["completed_trials"] == expected_trials
    for fixture, key in zip(parallel_fixtures, ("primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio"), strict=True):
        transport = key.split(":", 1)[1]
        expected_resets = 1 + sum(transport in task.fixture.transports for task in tasks) * manifest.repeats + 1
        assert fixture.reset_calls == expected_resets
    assert sum(parallel["timing"]["breakdown"].values()) == pytest.approx(
        parallel["timing"]["wall_seconds"]
    )
    assert parallel["artifact_hash_input"] == second_parallel["artifact_hash_input"]


@pytest.mark.asyncio
async def test_request_timeout_allows_sibling_lanes_and_finalizes_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    descriptor = _parallel_descriptor()
    resolver = _ParallelResolver()
    completed_lanes: list[str] = []
    failed_once = True
    checkpoint = tmp_path / "timeout.checkpoint.json"

    async def fake_preflight(_runner):
        return {
            "runtime": {
                "source_revision": manifest.arm_source_revisions["baseline"],
                "artifact_versions": {"backend_artifact_version": "0.0.0", "proxy_artifact_version": "0.0.0"},
                "discovery": {"status": "ready"},
            },
            "resolver": resolver,
        }

    async def fake_capture(*_args, **kwargs):
        return _catalog_snapshot(manifest, "baseline", kwargs["transport"]).model_copy(
            update={"source_revision": kwargs["source_revision"], "artifact_version": kwargs["artifact_version"]}
        )

    async def fake_smoke(task, *, model_spec, transport, **_kwargs):
        return _outcome(task, model_spec, transport, 1).model_copy(update={"locale": task.locale})

    async def fake_evaluate(tasks_for_repeat, *, executor, repeat_indices, checkpoint_sink, **_kwargs):
        nonlocal failed_once
        run_key = f"{executor.model_spec.class_name}:{executor.transport}"
        if run_key == "primary:http" and failed_once:
            failed_once = False
            task = tasks_for_repeat[0]
            timeout = _outcome(task, executor.model_spec, executor.transport, repeat_indices[task.id]).model_copy(
                update={
                    "locale": task.locale,
                    "error": "benchmark incomplete: provider request timeout",
                    "failure_kind": "request_timeout",
                    "model_requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "provider_evidence": [],
                    "provider_cost_usd": None,
                    "cost_source": "registered_price_snapshot",
                    "routing_observed": False,
                    "routing_valid": False,
                }
            )
            await checkpoint_sink(timeout, "failed")
            outcomes = [timeout]
            for remaining_task in tasks_for_repeat[1:]:
                outcome = _outcome(
                    remaining_task,
                    executor.model_spec,
                    executor.transport,
                    repeat_indices[remaining_task.id],
                ).model_copy(update={"locale": remaining_task.locale})
                await checkpoint_sink(outcome, "completed")
                outcomes.append(outcome)
            completed_lanes.append(run_key)
            return _ParallelReport(outcomes)

        await asyncio.sleep(0.2)
        outcomes = [
                _outcome(task, executor.model_spec, executor.transport, repeat_indices[task.id]).model_copy(
                    update={"locale": task.locale}
                )
                for task in tasks_for_repeat
        ]
        for outcome in outcomes:
            await checkpoint_sink(outcome, "completed")
        completed_lanes.append(run_key)
        return _ParallelReport(outcomes)

    monkeypatch.setattr(runner_module, "RuntimeFixture", _ParallelFixture)
    monkeypatch.setattr(BenchmarkRunner, "preflight", fake_preflight)
    async def fake_provider_registry(self, _ledger):
        self.provider_registry = provider_registry_snapshot(manifest)
        return self.provider_registry

    monkeypatch.setattr(BenchmarkRunner, "_capture_provider_registry", fake_provider_registry)
    monkeypatch.setattr(runner_module, "capture_catalog", fake_capture)
    monkeypatch.setattr(runner_module, "build_model", lambda spec: SimpleNamespace(settings={}, model_spec=spec))
    monkeypatch.setattr(runner_module, "execute_smoke", fake_smoke)
    monkeypatch.setattr(runner_module, "evaluate_dataset", fake_evaluate)
    monkeypatch.setattr(runner_module, "serialize_report", lambda *_args, **_kwargs: {})

    runner = BenchmarkRunner(manifest, tasks, descriptor, arm="baseline", checkpoint_path=checkpoint)
    artifact = await runner.run()

    raw = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert artifact["status"] == "incomplete"
    assert len(completed_lanes) == len(manifest.models) * len(manifest.transports) * manifest.repeats
    assert set(completed_lanes) == {"primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio"}
    assert raw["lifecycle"] == "finalized"
    assert raw["timing"]["active_attempt"] is None
    assert len(raw["timing"]["attempts"]) == 1
    assert len(raw["records"]) == artifact["completed_trials"]
    assert raw["reserved_cost_usd"] == 0
    assert artifact["checkpoint"]["lifecycle"] == "finalized"
    assert artifact["checkpoint"]["record_count"] == artifact["completed_trials"] == len(raw["records"])
    assert artifact["checkpoint"]["timing"] == raw["timing"]
    assert artifact["budget_used"]["reserved_cost_usd"] == 0
    for field in ("model_requests", "input_tokens", "output_tokens", "cost_usd", "wall_seconds", "model_work_seconds"):
        assert artifact["budget_used"][field] == raw["spent"][field]
    assert runner._checkpoint_store is not None
    with pytest.raises(Exception, match="late writes"):
        runner._checkpoint_store.record_trial(
            next(iter(runner._checkpoint_store.expected_keys.values())),
            _outcome(tasks[0], manifest.models[0], "http", 1).model_copy(update={"locale": tasks[0].locale}),
            status="failed",
        )
