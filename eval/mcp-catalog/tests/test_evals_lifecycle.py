from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.evidence import serialize_report
from mcp_catalog.execution import TrialOutcome, evaluate_dataset
from mcp_catalog.runtime import StateObservation

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
