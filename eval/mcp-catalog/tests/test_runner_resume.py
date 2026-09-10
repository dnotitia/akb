from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_catalog.runner as runner_module
from mcp_catalog.contracts import CatalogSnapshot, hash_json, load_run_manifest, load_task_corpus, token_estimate
from mcp_catalog.execution import TrialOutcome
from mcp_catalog.runner import BenchmarkRunner
from mcp_catalog.runtime import RuntimeDescriptor
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


class _RunResolver:
    def __init__(self) -> None:
        self.refreshes = 0

    def required_profiles(self, _tasks):
        return ["default"]

    async def prepare(self, _fixture, _profiles) -> None:
        return None

    def token_for(self, _profile: str) -> str:
        return "fixture-token"

    def secrets_for(self, _profile: str) -> tuple[str, ...]:
        return ("fixture-token",)

    def secret_values(self) -> tuple[str, ...]:
        return ("fixture-token",)

    async def refresh_after_reset(self, _fixture, _profile: str) -> str:
        self.refreshes += 1
        return f"fresh-token-{self.refreshes}"

    def mark_reset_complete(self) -> None:
        return None

    async def cleanup(self, _fixture) -> None:
        return None


class _RunFixture:
    instances: list[_RunFixture] = []

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


class _Report:
    def __init__(self, outcomes: list[TrialOutcome]) -> None:
        self.failures: list[object] = []
        self.cases = [SimpleNamespace(output=outcome) for outcome in outcomes]


def _valid_outcome(task, executor, repeat_index: int) -> TrialOutcome:
    model_id = executor.model_spec.model_id
    return TrialOutcome(
        task_id=task.id,
        category=task.category,
        arm=executor.arm,
        model_class=executor.model_spec.class_name,
        model_id=model_id,
        transport=executor.transport,
        repeat_index=repeat_index,
        final_answer_text="완료",
        successful_mcp_tool_calls=1,
        follow_up_terminal_response=True,
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        model_requests=2,
        cost_usd=0.00001,
        provider_evidence=[
            {
                "model": model_id,
                "routing": {"endpoints": {"available": [{"provider": "parasail", "selected": True}]}},
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        ],
        routing_observed=True,
        routing_valid=True,
    )


@pytest.mark.asyncio
async def test_runner_resume_reuses_completed_trials_and_preserves_hash_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loaded_manifest = load_run_manifest(ROOT / "config" / "run.json")
    manifest = loaded_manifest.model_copy(update={"repeats": 2})
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    checkpoint = tmp_path / "benchmark.checkpoint.json"
    resolver = _RunResolver()
    smoke_calls: list[str] = []
    evaluation_calls: list[str] = []

    async def fake_preflight(_self):
        return {
            "runtime": {
                "source_revision": "a" * 40,
                "artifact_versions": {
                    "backend_artifact_version": "0.0.0",
                    "proxy_artifact_version": "0.0.0",
                },
                "discovery": {"status": "ready"},
            },
            "resolver": resolver,
        }

    async def fake_capture(*_args, **_kwargs):
        tools = []
        return CatalogSnapshot(
            transport=_kwargs["transport"],
            source_revision="a" * 40,
            artifact_version="0.0.0",
            tool_count=0,
            catalog_hash=hash_json(tools),
            catalog_token_estimate=token_estimate(tools),
            tools=tools,
        )

    async def fake_smoke(task, *, model_spec, transport, **_kwargs):
        smoke_calls.append(f"{model_spec.class_name}:{transport}")
        return _valid_outcome(
            task,
            SimpleNamespace(
                arm="baseline",
                model_spec=model_spec,
                transport=transport,
            ),
            1,
        )

    async def fake_evaluate(tasks_for_repeat, *, executor, repeat_indices, checkpoint_sink, **_kwargs):
        evaluation_calls.append(f"{executor.model_spec.class_name}:{executor.transport}")
        outcomes = []
        for task in tasks_for_repeat:
            outcome = _valid_outcome(task, executor, repeat_indices[task.id])
            await checkpoint_sink(outcome, "completed")
            outcomes.append(outcome)
        return _Report(outcomes)

    monkeypatch.setattr(runner_module, "RuntimeFixture", _RunFixture)
    monkeypatch.setattr(BenchmarkRunner, "preflight", fake_preflight)
    monkeypatch.setattr(runner_module, "capture_catalog", fake_capture)
    monkeypatch.setattr(runner_module, "build_model", lambda spec: SimpleNamespace(settings={}, model_spec=spec))
    monkeypatch.setattr(runner_module, "execute_smoke", fake_smoke)
    monkeypatch.setattr(runner_module, "evaluate_dataset", fake_evaluate)
    monkeypatch.setattr(runner_module, "serialize_report", lambda *_args, **_kwargs: {})

    first = BenchmarkRunner(manifest, tasks, descriptor, arm="baseline", checkpoint_path=checkpoint)
    first_artifact = await first.run()
    assert first_artifact["status"] == "complete"
    assert first_artifact["completed_trials"] == 120
    assert first_artifact["checkpoint"]["new_trials"] == 120
    assert first_artifact["checkpoint"]["reused_trials"] == 0
    assert len(smoke_calls) == 4
    assert len(evaluation_calls) == 8

    smoke_calls.clear()
    evaluation_calls.clear()
    second = BenchmarkRunner(manifest, tasks, descriptor, arm="baseline", resume_path=checkpoint)
    second_artifact = await second.run()

    assert second_artifact["status"] == "complete"
    assert second_artifact["completed_trials"] == 120
    assert second_artifact["checkpoint"]["new_trials"] == 0
    assert second_artifact["checkpoint"]["reused_trials"] == 120
    assert second_artifact["checkpoint"]["rerun_trials"] == 0
    assert smoke_calls == []
    assert evaluation_calls == []
    assert first_artifact["artifact_hash"] == second_artifact["artifact_hash"]
    assert first_artifact["artifact_hash_input"] == second_artifact["artifact_hash_input"]
