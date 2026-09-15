from __future__ import annotations

from pathlib import Path

import pytest

import mcp_catalog.runner as runner_module
from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.execution import BudgetLedger, TrialOutcome
from mcp_catalog.runner import BenchmarkRunner, RuntimeContractError
from mcp_catalog.runtime import RuntimeDescriptor
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


class _SmokeResolver:
    def secret_values(self) -> tuple[str, ...]:
        return ()

    async def refresh_after_reset(self, _fixture, _profile: str) -> str:
        return "smoke-token"


class _SmokeFixture:
    def __init__(self) -> None:
        self.reset_calls = 0
        self.descriptor = RuntimeDescriptor.from_dict(descriptor_dict())

    async def reset(self) -> None:
        self.reset_calls += 1


@pytest.mark.asyncio
async def test_smoke_gate_executes_all_model_transport_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    runner = BenchmarkRunner(manifest, tasks, RuntimeDescriptor.from_dict(descriptor_dict()))
    fixture = _SmokeFixture()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())

    async def smoke(task, *, model_spec, transport, **_kwargs):
        calls.append((model_spec.class_name, transport))
        return TrialOutcome(
            task_id=task.id,
            category=task.category,
            arm="baseline",
            model_class=model_spec.class_name,
            model_id=model_spec.model_id,
            transport=transport,
            final_answer_text="OK",
            successful_mcp_tool_calls=1,
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
                        "endpoints": {"available": [{"provider": "OpenInference", "selected": True}]}
                    },
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001},
                }
            ],
            provider_cost_usd=0.00001,
            cost_source="provider_response",
            routing_observed=True,
            routing_valid=True,
        )

    monkeypatch.setattr(runner_module, "execute_smoke", smoke)
    result = await runner._run_smoke_gate(
        fixture=fixture,
        resolver=_SmokeResolver(),
        ledger=BudgetLedger(manifest),
    )

    assert result["status"] == "passed"
    assert calls == [
        (model.class_name, transport)
        for model in manifest.models
        for transport in manifest.transports
    ]
    assert fixture.reset_calls == 4
    for cell in result["cells"]:
        outcome = cell["outcome"]
        model_class, transport = cell["cell"].split(":", maxsplit=1)
        model_spec = next(model for model in manifest.models if model.class_name == model_class)
        assert outcome["model_id"] == model_spec.model_id
        assert outcome["transport"] == transport
        assert outcome["cost_source"] == "provider_response"
        assert outcome["provider_cost_usd"] > 0
        selected_provider = outcome["provider_evidence"][0]["routing"]["endpoints"]["available"][0]
        assert selected_provider == {"provider": "OpenInference", "selected": True}


@pytest.mark.asyncio
async def test_smoke_gate_rejects_outcome_for_a_different_requested_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    runner = BenchmarkRunner(manifest, tasks, RuntimeDescriptor.from_dict(descriptor_dict()))
    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())

    async def mismatched_smoke(task, *, model_spec, transport, **_kwargs):
        return TrialOutcome(
            task_id=task.id,
            category=task.category,
            arm="baseline",
            model_class=model_spec.class_name,
            model_id="unregistered/model",
            transport=transport,
            final_answer_text="OK",
            successful_mcp_tool_calls=1,
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
                        "endpoints": {"available": [{"provider": "OpenInference", "selected": True}]}
                    },
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001},
                }
            ],
            provider_cost_usd=0.00001,
            cost_source="provider_response",
            routing_observed=True,
            routing_valid=True,
        )

    monkeypatch.setattr(runner_module, "execute_smoke", mismatched_smoke)

    with pytest.raises(RuntimeContractError, match="smoke gate cell"):
        await runner._run_smoke_gate(
            fixture=_SmokeFixture(),
            resolver=_SmokeResolver(),
            ledger=BudgetLedger(manifest),
        )


@pytest.mark.asyncio
async def test_smoke_gate_blocks_when_cell_has_no_successful_mcp_call(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    runner = BenchmarkRunner(manifest, tasks, RuntimeDescriptor.from_dict(descriptor_dict()))
    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())

    async def failed_smoke(task, *, model_spec, transport, **_kwargs):
        return TrialOutcome(
            task_id=task.id,
            category=task.category,
            arm="baseline",
            model_class=model_spec.class_name,
            model_id=model_spec.model_id,
            transport=transport,
            final_answer_text="OK",
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
            model_requests=2,
            cost_usd=0.00001,
            provider_evidence=[
                {
                    "model": model_spec.model_id,
                    "routing": {
                        "endpoints": {"available": [{"provider": "OpenInference", "selected": True}]}
                    },
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001},
                }
            ],
            provider_cost_usd=0.00001,
            cost_source="provider_response",
            routing_observed=True,
            routing_valid=True,
        )

    monkeypatch.setattr(runner_module, "execute_smoke", failed_smoke)

    with pytest.raises(RuntimeContractError, match="smoke gate cell"):
        await runner._run_smoke_gate(
            fixture=_SmokeFixture(),
            resolver=_SmokeResolver(),
            ledger=BudgetLedger(manifest),
        )
