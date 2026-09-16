from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

import mcp_catalog.runner as runner_module
from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.execution import BudgetExceeded, BudgetLedger, TrialOutcome
from mcp_catalog.runner import BenchmarkRunner, RuntimeContractError
from mcp_catalog.runtime import RuntimeDescriptor
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


class _SmokeResolver:
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self._secrets = secrets

    def secret_values(self) -> tuple[str, ...]:
        return self._secrets

    async def refresh_after_reset(self, _fixture, _profile: str) -> str:
        return "smoke-token"


class _SmokeFixture:
    def __init__(self) -> None:
        self.reset_calls = 0
        self.descriptor = RuntimeDescriptor.from_dict(descriptor_dict())

    async def reset(self) -> None:
        self.reset_calls += 1


def _smoke_outcome(
    task,
    model_spec,
    transport: str,
    *,
    model_id: str | None = None,
    successful_mcp_tool_calls: int = 1,
    follow_up_terminal_response: bool = True,
) -> TrialOutcome:
    return TrialOutcome(
        task_id=task.id,
        category=task.category,
        arm="baseline",
        model_class=model_spec.class_name,
        model_id=model_id or model_spec.model_id,
        transport=transport,
        final_answer_text="OK",
        successful_mcp_tool_calls=successful_mcp_tool_calls,
        follow_up_terminal_response=follow_up_terminal_response,
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
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.000005},
            }
            for _ in range(2)
        ],
        provider_cost_usd=0.00001,
        cost_source="provider_response",
        routing_observed=True,
        routing_valid=True,
    )


def _parallel_descriptor() -> RuntimeDescriptor:
    descriptor = descriptor_dict()
    cell_names = ("primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio")
    descriptor["benchmark_cells"] = {name: descriptor_dict() for name in cell_names}
    return RuntimeDescriptor.from_dict(descriptor)


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
        return _smoke_outcome(task, model_spec, transport)

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
        return _smoke_outcome(
            task,
            model_spec,
            transport,
            model_id="unregistered/model",
        )

    monkeypatch.setattr(runner_module, "execute_smoke", mismatched_smoke)

    with pytest.raises(RuntimeContractError, match="smoke gate cell"):
        await runner._run_smoke_gate(
            fixture=_SmokeFixture(),
            resolver=_SmokeResolver(),
            ledger=BudgetLedger(manifest),
        )


@pytest.mark.asyncio
async def test_smoke_gate_requires_provider_evidence_for_each_model_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    runner = BenchmarkRunner(manifest, tasks, RuntimeDescriptor.from_dict(descriptor_dict()))
    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())

    async def missing_terminal_request_evidence(task, *, model_spec, transport, **_kwargs):
        outcome = _smoke_outcome(task, model_spec, transport)
        return outcome.model_copy(update={"provider_evidence": outcome.provider_evidence[:1]})

    monkeypatch.setattr(runner_module, "execute_smoke", missing_terminal_request_evidence)

    with pytest.raises(RuntimeContractError, match="smoke gate cell"):
        await runner._run_smoke_gate(
            fixture=_SmokeFixture(),
            resolver=_SmokeResolver(),
            ledger=BudgetLedger(manifest),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_usage",
    [
        {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0},
        {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.000005},
    ],
)
async def test_smoke_gate_rejects_response_without_positive_usage_and_cost(
    monkeypatch: pytest.MonkeyPatch,
    invalid_usage: dict[str, float | int],
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    runner = BenchmarkRunner(manifest, tasks, RuntimeDescriptor.from_dict(descriptor_dict()))
    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())

    async def incomplete_usage_smoke(task, *, model_spec, transport, **_kwargs):
        outcome = _smoke_outcome(task, model_spec, transport)
        evidence = list(outcome.provider_evidence)
        evidence[1] = {**evidence[1], "usage": invalid_usage}
        return outcome.model_copy(update={"provider_evidence": evidence})

    monkeypatch.setattr(runner_module, "execute_smoke", incomplete_usage_smoke)

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
        return _smoke_outcome(
            task,
            model_spec,
            transport,
            successful_mcp_tool_calls=0,
            follow_up_terminal_response=False,
        )

    monkeypatch.setattr(runner_module, "execute_smoke", failed_smoke)

    with pytest.raises(RuntimeContractError, match="smoke gate cell"):
        await runner._run_smoke_gate(
            fixture=_SmokeFixture(),
            resolver=_SmokeResolver(),
            ledger=BudgetLedger(manifest),
        )


@pytest.mark.asyncio
async def test_smoke_accounting_failure_is_checkpointed_and_resume_reuses_other_cells(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    checkpoint_path = tmp_path / "smoke-checkpoint.json"
    secret = "fixture-smoke-secret"
    resolver = _SmokeResolver((secret,))
    failed_cell = "lightweight:stdio"
    source_revision = "a" * 40
    required_cells = [
        f"{model.class_name}:{transport}"
        for model in manifest.models
        for transport in manifest.transports
    ]

    first_runner = BenchmarkRunner(
        manifest,
        tasks,
        _parallel_descriptor(),
        checkpoint_path=checkpoint_path,
    )
    first_runner._checkpoint_store = first_runner._checkpoint_store_for(
        source_revision=source_revision,
        resolver=resolver,
        planned_keys={},
    )
    first_ledger = BudgetLedger(manifest)
    first_runner._ledger = first_ledger
    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())
    first_calls: list[str] = []
    failure_recorded_for_sibling = asyncio.Event()
    first_store = first_runner._checkpoint_store
    assert first_store is not None

    async def wait_for_failed_cell_checkpoint() -> None:
        while True:
            if checkpoint_path.is_file():
                payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if failed_cell in payload.get("smoke_gate", {}):
                    return
            await asyncio.sleep(0.001)

    async def smoke_with_accounting_failure(
        task,
        *,
        model_spec,
        transport,
        request_guard,
        **_kwargs,
    ):
        cell = f"{model_spec.class_name}:{transport}"
        first_calls.append(cell)
        if cell == "primary:http":
            await asyncio.wait_for(wait_for_failed_cell_checkpoint(), timeout=2.0)
            failure_recorded_for_sibling.set()
        if cell == failed_cell:
            receipt = await request_guard()
            try:
                await first_ledger.record_provider_response_cost(
                    request_guard,
                    receipt,
                    Decimal("0.101"),
                )
            except BudgetExceeded:
                base = _smoke_outcome(task, model_spec, transport)
                evidence = dict(base.provider_evidence[0])
                evidence["diagnostic"] = secret
                evidence["usage"] = {
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "cost": 0.101,
                }
                return base.model_copy(
                    update={
                        "input_tokens": 5,
                        "output_tokens": 1,
                        "total_tokens": 6,
                        "model_requests": 1,
                        "cost_usd": 0.101,
                        "provider_cost_usd": 0.101,
                        "provider_evidence": [evidence],
                        "failure_kind": "budget",
                    }
                )
            raise AssertionError("overspend response should fail provider cost admission")

        for _ in range(2):
            receipt = await request_guard()
            await first_ledger.record_provider_response_cost(
                request_guard,
                receipt,
                Decimal("0.000005"),
            )
        return _smoke_outcome(task, model_spec, transport)

    monkeypatch.setattr(runner_module, "execute_smoke", smoke_with_accounting_failure)
    with pytest.raises(RuntimeContractError, match="smoke gate cell"):
        await first_runner._run_smoke_gate(
            fixture=_SmokeFixture(),
            resolver=resolver,
            ledger=first_ledger,
        )

    assert set(first_calls) == set(required_cells)
    assert failure_recorded_for_sibling.is_set()
    assert first_store.document.smoke_status == "failed"
    assert set(first_store.document.smoke_gate) == set(required_cells)
    failed = first_store.document.smoke_gate[failed_cell]
    assert failed.status == "incomplete"
    assert failed.outcome.model_requests == 1
    assert failed.outcome.provider_cost_usd == pytest.approx(0.101)
    assert failed.outcome.provider_evidence[0]["usage"]["cost"] == pytest.approx(0.101)
    assert failed.outcome.provider_evidence[0]["diagnostic"] == "[redacted]"
    assert first_store.document.spent.model_requests == 7
    assert first_store.document.spent.cost_usd == pytest.approx(0.10103)
    assert first_store.document.reserved_cost_usd == 0
    assert first_ledger.reserved_cost_usd == Decimal("0")

    resumed_runner = BenchmarkRunner(
        manifest,
        tasks,
        _parallel_descriptor(),
        resume_path=checkpoint_path,
    )
    resumed_runner._checkpoint_store = resumed_runner._checkpoint_store_for(
        source_revision=source_revision,
        resolver=resolver,
        planned_keys={},
    )
    resumed_store = resumed_runner._checkpoint_store
    assert resumed_store is not None
    spent = resumed_store.document.spent
    resumed_ledger = BudgetLedger(manifest)
    resumed_ledger.restore(
        model_requests=spent.model_requests,
        input_tokens=spent.input_tokens,
        output_tokens=spent.output_tokens,
        cost_usd=spent.cost_usd,
        wall_seconds=spent.wall_seconds,
        model_work_seconds=spent.model_work_seconds,
        budget_failure=resumed_store.budget_failure_reason(),
    )
    resumed_runner._ledger = resumed_ledger
    resumed_calls: list[str] = []

    async def retry_accounting_cell(task, *, model_spec, transport, request_guard, **_kwargs):
        cell = f"{model_spec.class_name}:{transport}"
        resumed_calls.append(cell)
        for _ in range(2):
            receipt = await request_guard()
            await resumed_ledger.record_provider_response_cost(
                request_guard,
                receipt,
                Decimal("0.000005"),
            )
        return _smoke_outcome(task, model_spec, transport)

    monkeypatch.setattr(runner_module, "execute_smoke", retry_accounting_cell)
    result = await resumed_runner._run_smoke_gate(
        fixture=_SmokeFixture(),
        resolver=resolver,
        ledger=resumed_ledger,
    )

    assert result["status"] == "passed"
    assert resumed_calls == [failed_cell]
    assert {cell["cell"] for cell in result["cells"] if cell["reused"]} == set(required_cells) - {failed_cell}
    assert all(record.status == "completed" for record in resumed_store.document.smoke_gate.values())
    assert resumed_store.document.smoke_status == "passed"
    assert resumed_ledger.reserved_cost_usd == Decimal("0")
