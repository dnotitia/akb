from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_catalog.runner as runner_module
from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.execution import BudgetExceeded, BudgetLedger, TrialOutcome
from mcp_catalog.runner import BenchmarkRunner, PairedArmCoordinator, planned_arm_order
from mcp_catalog.runtime import RuntimeContractError
from paired_artifact_factory import provider_registry_snapshot


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
        runner.provider_registry = provider_registry_snapshot(manifest)
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
async def test_paired_continuation_appends_after_parent_execution_sequence() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(item for item in load_task_corpus(ROOT / "corpus" / "tasks.json") if item.id == "read-vaults-en")
    cell = "primary:http"
    first_arm, second_arm = planned_arm_order(task.id, 1, manifest.paired_order_seed)
    parent_event = {
        "sequence": 1,
        "cell": cell,
        "task_id": task.id,
        "repeat_index": 1,
        "arm": first_arm,
        "order_position": 0,
    }
    coordinator = PairedArmCoordinator(manifest, [task], initial_events=[parent_event])
    parent_outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        arm=first_arm,
        model_class="primary",
        model_id=manifest.models[0].model_id,
        transport="http",
        repeat_index=1,
        paired_order_position=0,
        paired_execution_sequence=1,
    )
    await coordinator.register_arm(first_arm, {(cell, task.id, 1): parent_outcome})
    await coordinator.register_arm(second_arm, {})

    async with coordinator.turn(cell=cell, task_id=task.id, repeat_index=1, arm=second_arm) as turn:
        assert turn.execution_sequence == 2
        assert turn.order_position == 1

    events = coordinator.evidence()["events"]
    assert [item["sequence"] for item in events] == [1, 2]
    assert events[0] == parent_event


@pytest.mark.asyncio
async def test_tighter_continuation_cost_limit_applies_to_restored_parent_spend() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    ledger = BudgetLedger(manifest, total_cost_limit_usd=Decimal("5.0"))

    await ledger.restore_additive(
        model_requests=1,
        input_tokens=0,
        output_tokens=0,
        cost_usd=Decimal("4.9"),
        wall_seconds=0,
    )
    with pytest.raises(BudgetExceeded, match="paired checkpoint totals already exceed"):
        await ledger.restore_additive(
            model_requests=1,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0.1001"),
            wall_seconds=0,
        )

    assert manifest.budget.max_total_cost_usd == 50.0


@pytest.mark.asyncio
async def test_single_request_timeout_does_not_stop_next_paired_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = [
        task
        for task in load_task_corpus(ROOT / "corpus" / "tasks.json")
        if "http" in task.fixture.transports
    ][:2]
    coordinator = PairedArmCoordinator(manifest, tasks)
    await coordinator.register_arm("baseline", {})
    await coordinator.register_arm("candidate", {})
    calls: list[tuple[str, str, int, int]] = []
    timeout_arm = planned_arm_order(tasks[0].id, 1, manifest.paired_order_seed)[0]

    async def fake_evaluate(selected_tasks, *, executor, repeat_indices, **_kwargs):
        task = selected_tasks[0]
        calls.append((executor.arm, task.id, executor.order_position, executor.execution_sequence))
        timed_out = task.id == tasks[0].id and executor.arm == timeout_arm
        outcome = TrialOutcome(
            task_id=task.id,
            category=task.category,
            locale=task.locale,
            arm=executor.arm,
            model_class=executor.model_spec.class_name,
            model_id=executor.model_spec.model_id,
            transport="http",
            repeat_index=repeat_indices[task.id],
            paired_order_position=executor.order_position,
            paired_execution_sequence=executor.execution_sequence,
            error="benchmark incomplete: provider request timeout" if timed_out else None,
            failure_kind="request_timeout" if timed_out else "none",
            model_requests=1 if timed_out else 2,
        )
        return SimpleNamespace(failures=[], cases=[SimpleNamespace(output=outcome)])

    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())
    monkeypatch.setattr(runner_module, "TrialExecutor", _Executor)
    monkeypatch.setattr(runner_module, "evaluate_dataset", fake_evaluate)
    monkeypatch.setattr(runner_module, "serialize_report", lambda _report, _secrets: {})

    resolver = _Resolver()
    model_spec = manifest.models[0]

    async def run_arm(arm: str):
        runner = BenchmarkRunner(manifest, tasks, object(), arm=arm, paired_coordinator=coordinator)
        runner.provider_registry = provider_registry_snapshot(manifest)
        runner._resolver = resolver  # type: ignore[assignment]
        incomplete_reasons: set[str] = set()
        await runner._run_cell(
            run_key=f"{model_spec.class_name}:http",
            model_spec=model_spec,
            transport="http",
            pending=[(task, 1, None) for task in tasks],  # type: ignore[list-item]
            fixture=object(),  # type: ignore[arg-type]
            resolver=resolver,  # type: ignore[arg-type]
            ledger=object(),  # type: ignore[arg-type]
            planned_by_identity={},
            profiles=["default"],
            lifecycle_cleanup_errors=[],
            incomplete_reasons=incomplete_reasons,
            reports={},
        )
        return runner._completed_trials[f"{model_spec.class_name}:http"], incomplete_reasons

    results = await asyncio.gather(run_arm("baseline"), run_arm("candidate"))

    assert len(calls) == len(tasks) * 2
    for task in tasks:
        observed_arms = [arm for arm, task_id, _position, _sequence in calls if task_id == task.id]
        assert observed_arms == list(planned_arm_order(task.id, 1, manifest.paired_order_seed))
    timeout_trials = [trial for trials, _reasons in results for trial in trials if trial.failure_kind == "request_timeout"]
    assert len(timeout_trials) == 1
    assert timeout_trials[0].task_id == tasks[0].id
    assert any("provider request timeout" in reason for _trials, reasons in results for reason in reasons)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "error", "provider_evidence", "routing_valid", "unsafe_mutation", "continues"),
    [
        ("budget", "benchmark incomplete: max_total_cost_usd exceeded", [], False, False, False),
        ("interrupted", "benchmark incomplete: benchmark interrupted", [], False, False, False),
        ("provider", "provider route drift", [{"model": "unregistered/model"}], False, False, False),
        ("none", None, [], False, True, False),
        ("provider", "OpenRouter routing evidence was not returned", [], False, False, True),
    ],
)
async def test_terminal_trial_boundaries_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    error: str | None,
    provider_evidence: list[dict[str, object]],
    routing_valid: bool,
    unsafe_mutation: bool,
    continues: bool,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = [
        task
        for task in load_task_corpus(ROOT / "corpus" / "tasks.json")
        if "http" in task.fixture.transports
    ][:2]
    calls: list[str] = []

    async def fake_evaluate(selected_tasks, *, executor, repeat_indices, **_kwargs):
        task = selected_tasks[0]
        calls.append(task.id)
        outcome = TrialOutcome(
            task_id=task.id,
            category=task.category,
            locale=task.locale,
            arm=executor.arm,
            model_class=executor.model_spec.class_name,
            model_id=executor.model_spec.model_id,
            transport="http",
            repeat_index=repeat_indices[task.id],
            error=error,
            failure_kind=failure_kind,
            provider_evidence=provider_evidence,
            routing_valid=routing_valid,
            unsafe_mutation=unsafe_mutation,
        )
        return SimpleNamespace(failures=[], cases=[SimpleNamespace(output=outcome)])

    monkeypatch.setattr(runner_module, "build_model", lambda _spec: object())
    monkeypatch.setattr(runner_module, "TrialExecutor", _Executor)
    monkeypatch.setattr(runner_module, "evaluate_dataset", fake_evaluate)
    monkeypatch.setattr(runner_module, "serialize_report", lambda _report, _secrets: {})

    resolver = _Resolver()
    model_spec = manifest.models[0]
    runner = BenchmarkRunner(manifest, tasks, object(), arm="baseline")
    runner.provider_registry = provider_registry_snapshot(manifest)
    runner._resolver = resolver  # type: ignore[assignment]
    run_key = f"{model_spec.class_name}:http"

    async def run_cell() -> None:
        await runner._run_cell(
            run_key=run_key,
            model_spec=model_spec,
            transport="http",
            pending=[(tasks[0], 1, None), (tasks[1], 2, None)],  # type: ignore[list-item]
            fixture=object(),  # type: ignore[arg-type]
            resolver=resolver,  # type: ignore[arg-type]
            ledger=object(),  # type: ignore[arg-type]
            planned_by_identity={},
            profiles=["default"],
            lifecycle_cleanup_errors=[],
            incomplete_reasons=set(),
            reports={},
        )

    if continues:
        await run_cell()
        assert calls == [tasks[0].id, tasks[1].id]
    else:
        with pytest.raises(RuntimeContractError):
            await run_cell()
        assert calls == [tasks[0].id]


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


@pytest.mark.asyncio
async def test_paired_resume_rejects_completed_trials_after_a_gap() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = [
        task
        for task in load_task_corpus(ROOT / "corpus" / "tasks.json")
        if "http" in task.fixture.transports
    ][:3]
    coordinator = PairedArmCoordinator(manifest, tasks)
    cell = f"{manifest.models[0].class_name}:http"
    reused: dict[str, dict[tuple[str, str, int], TrialOutcome]] = {"baseline": {}, "candidate": {}}

    for task_index in (0, 2):
        task = tasks[task_index]
        for arm in ("baseline", "candidate"):
            order = planned_arm_order(task.id, 1, manifest.paired_order_seed)
            reused[arm][(cell, task.id, 1)] = TrialOutcome(
                task_id=task.id,
                category=task.category,
                locale=task.locale,
                arm=arm,
                model_class=manifest.models[0].class_name,
                model_id=manifest.models[0].model_id,
                transport="http",
                repeat_index=1,
                paired_order_position=order.index(arm),
            )

    await coordinator.register_arm("baseline", reused["baseline"])
    with pytest.raises(RuntimeContractError, match="completed prefix"):
        await coordinator.register_arm("candidate", reused["candidate"])
