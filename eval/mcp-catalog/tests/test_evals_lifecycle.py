from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.evidence import serialize_report
from mcp_catalog.execution import TrialOutcome, evaluate_dataset
from mcp_catalog.runtime import RuntimeContractError, RuntimeFixture, StateObservation
from test_runtime_contract import _TimedResetClient, descriptor_dict

ROOT = Path(__file__).parents[1]


class FakeFixture:
    def __init__(self) -> None:
        self.reset_calls = 0
        self.observe_calls = 0

    async def reset(self) -> None:
        self.reset_calls += 1

    async def observe(self, _probe, *, token: str) -> StateObservation:
        self.observe_calls += 1
        return StateObservation(True, 200, {"items": [], "owner": "fixture"})


class _ReadinessClient:
    def __init__(self, descriptor) -> None:
        self.descriptor = descriptor
        self.health_reads = 0
        self.ready = False

    async def post(self, _url: str, *, json: dict, **_kwargs: object) -> object:
        assert json == self.descriptor.reset_body
        self.health_reads = 0
        self.ready = False
        return type("Response", (), {"status_code": 200, "json": lambda _self: {"status": "ready", "scenario": self.descriptor.scenario}})()

    async def get(self, url: str, *, timeout: float | None = None, **_kwargs: object) -> object:
        assert url in {self.descriptor.app_health_url, self.descriptor.fixture_health_url}
        assert timeout is not None
        self.health_reads += 1
        ready = self.health_reads > 2
        if ready:
            self.ready = True
        return type("Response", (), {"status_code": 200, "json": lambda _self: {"status": "ready" if ready else "starting", "scenario": self.descriptor.scenario}})()

    async def aclose(self) -> None:
        return None


@dataclass
class _ReadinessGuardExecutor:
    manifest: object
    fixture_client: _ReadinessClient
    arm: str = "baseline"
    transport: str = "http"

    def __post_init__(self) -> None:
        self.model_spec = self.manifest.models[0]
        self.provider_calls = 0

    def token_for(self, _profile: str) -> str:
        return "fixture-token"

    def secrets_for(self, _profile: str) -> tuple[str, ...]:
        return ("fixture-token",)

    async def execute(self, task):
        assert self.fixture_client.ready, "provider execution must wait for runtime readiness"
        self.provider_calls += 1
        return TrialOutcome(
            task_id=task.id,
            category=task.category,
            arm=self.arm,
            model_class=self.model_spec.class_name,
            model_id=self.model_spec.model_id,
            transport=self.transport,
            final_answer_text="확인 전에는 변경하지 않습니다.",
            first_logical_operation="none",
        )


class _NeverReadyFixture(FakeFixture):
    async def reset(self) -> None:
        self.reset_calls += 1
        raise RuntimeContractError("fixture readiness did not recover", stage="fixture_readiness")

    async def observe(self, _probe, *, token: str) -> StateObservation:
        raise AssertionError("state observation must not run before readiness")


class _FailsBeforeNextTrialFixture(FakeFixture):
    async def reset(self) -> None:
        self.reset_calls += 1
        if self.reset_calls == 2:
            raise RuntimeContractError("fixture readiness failed before next trial", stage="fixture_readiness")


class _NotReadyMarker:
    ready = False


@dataclass
class FakeExecutor:
    manifest: object
    arm: str = "baseline"
    transport: str = "http"

    def __post_init__(self) -> None:
        self.model_spec = self.manifest.models[0]

    def token_for(self, _profile: str) -> str:
        return "fixture-token"

    def secrets_for(self, _profile: str) -> tuple[str, ...]:
        return ("fixture-token",)

    async def execute(self, task):
        return TrialOutcome(
            task_id=task.id,
            category=task.category,
            arm=self.arm,
            model_class=self.model_spec.class_name,
            model_id=self.model_spec.model_id,
            transport=self.transport,
            final_answer_text="확인 전에는 변경하지 않습니다.",
            first_logical_operation="none",
        )


@pytest.mark.asyncio
async def test_pydantic_evals_repeat_uses_case_lifecycle_for_reset_and_report() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "destructive-confirm-a")
    fixture = FakeFixture()
    executor = FakeExecutor(manifest)

    report = await evaluate_dataset([task], manifest=manifest, executor=executor, fixture=fixture)

    assert len(report.cases) == manifest.repeats
    assert not report.failures
    assert fixture.reset_calls == manifest.repeats * 2
    assert fixture.observe_calls == manifest.repeats * 2
    assert report.cases[0].assertions["success"].value is True
    assert report.analyses
    serialized = serialize_report(report, ("fixture-token",))
    assert serialized["cases"][0]["output"]["success"] is True


@pytest.mark.asyncio
async def test_provider_is_not_called_until_reset_readiness_recovers() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "destructive-confirm-a")
    from mcp_catalog.runtime import RuntimeDescriptor

    runtime_descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    fixture = RuntimeFixture(runtime_descriptor, readiness_timeout=0.2, readiness_poll_interval=0)
    client = _ReadinessClient(runtime_descriptor)
    fixture.client = client  # type: ignore[assignment]

    async def observe(_probe, *, token: str) -> StateObservation:
        assert token == "fixture-token"
        assert client.ready
        return StateObservation(True, 200, {"items": [], "owner": "fixture"})

    fixture.observe = observe  # type: ignore[method-assign]
    executor = _ReadinessGuardExecutor(manifest, client)
    try:
        report = await evaluate_dataset([task], manifest=manifest, executor=executor, fixture=fixture)
    finally:
        await fixture.close()

    assert not report.failures
    assert executor.provider_calls == manifest.repeats


@pytest.mark.asyncio
async def test_failed_reset_preserves_readiness_stage_and_blocks_provider() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "destructive-confirm-a")
    fixture = _NeverReadyFixture()
    executor = _ReadinessGuardExecutor(manifest, _NotReadyMarker())
    failures: list[Exception] = []
    cleanup_errors: list[Exception] = []

    report = await evaluate_dataset(
        [task],
        manifest=manifest,
        executor=executor,
        fixture=fixture,
        failure_sink=failures,
        cleanup_sink=cleanup_errors,
    )

    assert report.failures
    assert failures and isinstance(failures[0], RuntimeContractError)
    assert failures[0].stage == "fixture_readiness"
    assert cleanup_errors and cleanup_errors[0].stage == "fixture_readiness"
    assert executor.provider_calls == 0


@pytest.mark.asyncio
async def test_teardown_reset_blocks_the_next_trial() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "destructive-confirm-a")
    fixture = _FailsBeforeNextTrialFixture()
    marker = _NotReadyMarker()
    marker.ready = True
    executor = _ReadinessGuardExecutor(manifest, marker)
    failures: list[Exception] = []

    with pytest.raises(RuntimeContractError, match="before next trial") as raised:
        await evaluate_dataset([task], manifest=manifest, executor=executor, fixture=fixture, failure_sink=failures)

    assert raised.value.stage == "fixture_readiness"
    assert failures and failures[0] is raised.value
    assert executor.provider_calls == 1


@pytest.mark.asyncio
async def test_reset_timeout_blocks_provider_execution() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "destructive-confirm-a")
    from mcp_catalog.runtime import RuntimeDescriptor

    runtime_descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    fixture = RuntimeFixture(
        runtime_descriptor,
        timeout=0.01,
        reset_timeout=0.05,
        readiness_timeout=0.05,
        readiness_poll_interval=0,
    )
    client = _TimedResetClient(runtime_descriptor, response_delay=0.1)
    fixture.client = client  # type: ignore[assignment]
    failures: list[Exception] = []
    executor = _ReadinessGuardExecutor(manifest, _NotReadyMarker())

    try:
        report = await evaluate_dataset(
            [task],
            manifest=manifest,
            executor=executor,
            fixture=fixture,
            failure_sink=failures,
        )
    finally:
        await fixture.close()

    assert report.failures
    assert failures and isinstance(failures[0], RuntimeContractError)
    assert failures[0].stage == "fixture_reset"
    assert executor.provider_calls == 0
