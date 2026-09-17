from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_catalog.runner as runner_module
from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.execution import BudgetExceeded, BudgetLedger, TrialOutcome
from mcp_catalog.runner import BenchmarkRunner, PairedArmCoordinator, planned_arm_order
from mcp_catalog.runtime import RuntimeContractError


ROOT = Path(__file__).parents[1]


class _Executor:
    def __init__(self, _manifest, *, arm: str, model_spec, transport: str, **_kwargs) -> None:
        self.arm = arm
        self.model_spec = model_spec
        self.transport = transport
        self.order_position: int | None = None
        self.execution_sequence: int | None = None

    def set_paired_turn(self, *, order_position: int, execution_sequence: int) -> None:
        self.order_position = order_position
        self.execution_sequence = execution_sequence

    def clear_paired_turn(self) -> None:
        self.order_position = None
        self.execution_sequence = None


class _Resolver:
    def token_for(self, _profile: str) -> str:
        return "token"

    def secrets_for(self, _profile: str) -> tuple[str, ...]:
        return ()

    def mark_reset_complete(self) -> None:
        return None

    def secret_values(self) -> tuple[str, ...]:
        return ()


@pytest.mark.asyncio
async def test_runner_applies_counterbalanced_order_to_real_evaluation_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(item for item in load_task_corpus(ROOT / "corpus" / "tasks.json") if item.id == "read-vaults-en")
    coordinator = PairedArmCoordinator(manifest, [task])
    await coordinator.register_arm("baseline", {})
    await coordinator.register_arm("candidate", {})
    calls: list[tuple[int, str, int, int]] = []

    async def fake_evaluate(tasks, *, executor, repeat_indices, **_kwargs):
        await asyncio.sleep(0)
        selected = tasks[0]
        repeat_index = repeat_indices[selected.id]
        assert executor.order_position is not None
        assert executor.execution_sequence is not None
        calls.append(
            (
                repeat_index,
                executor.arm,
                executor.order_position,
                executor.execution_sequence,
            )
        )
        outcome = TrialOutcome(
            task_id=selected.id,
            category=selected.category,
            locale=selected.locale,
            arm=executor.arm,
            model_class=executor.model_spec.class_name,
            model_id=executor.model_spec.model_id,
            transport=executor.transport,
            repeat_index=repeat_index,
            paired_order_position=executor.order_position,
            paired_execution_sequence=executor.execution_sequence,
        )
        return SimpleNamespace(failures=[], cases=[SimpleNamespace(output=outcome)])

    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())
    monkeypatch.setattr(runner_module, "TrialExecutor", _Executor)
    monkeypatch.setattr(runner_module, "evaluate_dataset", fake_evaluate)
    monkeypatch.setattr(runner_module, "serialize_report", lambda _report, _secrets: {})

    resolver = _Resolver()
    model_spec = manifest.models[0]
    pending = [(task, repeat_index, None) for repeat_index in range(1, manifest.repeats + 1)]

    async def run_arm(arm: str) -> None:
        runner = BenchmarkRunner(
            manifest,
            [task],
            object(),  # type: ignore[arg-type]
            arm=arm,
            paired_coordinator=coordinator,
        )
        runner._resolver = resolver  # type: ignore[assignment]
        await runner._run_cell(
            run_key=f"{model_spec.class_name}:http",
            model_spec=model_spec,
            transport="http",
            pending=pending,  # type: ignore[arg-type]
            fixture=object(),  # type: ignore[arg-type]
            resolver=resolver,  # type: ignore[arg-type]
            ledger=object(),  # type: ignore[arg-type]
            planned_by_identity={},
            profiles=["default"],
            lifecycle_cleanup_errors=[],
            incomplete_reasons=set(),
            reports={},
        )

    await asyncio.gather(run_arm("baseline"), run_arm("candidate"))

    for repeat_index in range(1, manifest.repeats + 1):
        observed = [arm for repeat, arm, _position, _sequence in calls if repeat == repeat_index]
        expected = list(planned_arm_order(task.id, repeat_index, manifest.paired_order_seed))
        assert observed == expected
        positions = [position for repeat, _arm, position, _sequence in calls if repeat == repeat_index]
        assert positions == [0, 1]


@pytest.mark.asyncio
async def test_paired_resume_adds_both_arm_spend_to_one_global_cap() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    ledger = BudgetLedger(manifest)

    await ledger.restore_additive(
        model_requests=10,
        input_tokens=100,
        output_tokens=20,
        cost_usd=30,
        wall_seconds=5,
    )
    with pytest.raises(BudgetExceeded, match="paired checkpoint totals"):
        await ledger.restore_additive(
            model_requests=10,
            input_tokens=100,
            output_tokens=20,
            cost_usd=21,
            wall_seconds=6,
        )

    assert ledger.cost_usd == 30


@pytest.mark.asyncio
async def test_paired_resume_rejects_second_arm_without_completed_first_arm() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(item for item in load_task_corpus(ROOT / "corpus" / "tasks.json") if item.id == "read-vaults-en")
    coordinator = PairedArmCoordinator(manifest, [task])
    first_arm, second_arm = planned_arm_order(task.id, 1, manifest.paired_order_seed)
    cell = f"{manifest.models[0].class_name}:http"
    reused_second = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm=second_arm,
        model_class=manifest.models[0].class_name,
        model_id=manifest.models[0].model_id,
        transport="http",
        repeat_index=1,
        paired_order_position=1,
    )

    await coordinator.register_arm(first_arm, {})
    with pytest.raises(RuntimeContractError, match="completed prefix"):
        await coordinator.register_arm(second_arm, {(cell, task.id, 1): reused_second})
