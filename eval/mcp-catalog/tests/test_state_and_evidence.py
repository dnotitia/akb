from __future__ import annotations

import json
from pathlib import Path

import pytest

import mcp_catalog.evidence as evidence
from mcp_catalog.contracts import StateCheckpointContract, StateContract, StateExpectation, StateProbe, load_task_corpus
from mcp_catalog.evidence import redact_exception, safe_json, write_json
from mcp_catalog.execution import TrialOutcome, has_measured_evidence
from mcp_catalog.runtime import StateObservation
from mcp_catalog.state import evaluate_checkpoint_contract, evaluate_state_contract

ROOT = Path(__file__).parents[1]


def _contract(
    *,
    must=(),
    must_not=(),
    unchanged=(),
    expected_status: int = 200,
    before_expected_status: int | None = None,
) -> StateContract:
    return StateContract(
        probe=StateProbe(service="app", path="/state", expected_status=expected_status),
        before_expected_status=before_expected_status,
        must=list(must),
        must_not=list(must_not),
        unchanged=list(unchanged),
    )


def test_state_contract_checks_final_state_and_unchanged_paths() -> None:
    contract = _contract(
        must=(StateExpectation(pointer="/items", operator="contains", value={"name": "ok"}),),
        unchanged=("/owner",),
    )
    before = StateObservation(True, 200, {"owner": "fixture", "items": []})
    after = StateObservation(True, 200, {"owner": "fixture", "items": [{"name": "ok", "id": 1}]})

    passed, checks = evaluate_state_contract(contract, before, after)

    assert passed
    assert all(check.passed for check in checks)


def test_state_contract_supports_nested_json_pointers() -> None:
    contract = _contract(
        must=(StateExpectation(pointer="/items/0/name", operator="equals", value="ok"),),
    )
    observation = StateObservation(True, 200, {"items": [{"name": "ok"}]})

    passed, _ = evaluate_state_contract(contract, observation, observation)

    assert passed


def test_state_contract_fails_closed_when_observation_is_unavailable() -> None:
    contract = _contract(unchanged=("/items",))
    unavailable = StateObservation(False, 503, error="state probe unavailable")

    passed, checks = evaluate_state_contract(contract, unavailable, unavailable)

    assert not passed
    assert checks[0].reason == "state probe unavailable"


def test_json_resource_404_is_measured_but_fails_expected_200_contract() -> None:
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "table-publication-en")
    before = [
        StateObservation(True, item.resolved_before_expected_status, {})
        if item.check_before
        else StateObservation(True, None, None)
        for item in task.expected_final_state.observation_sets
    ]
    after = [
        StateObservation(True, 404, {"code": "not_found", "detail": "Vault not found: catalog-bench-overlap"})
        if index == 0
        else StateObservation(True, item.probe.expected_status, {})
        for index, item in enumerate(task.expected_final_state.observation_sets)
    ]
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="deepseek/deepseek-v4-flash-0731",
        transport="http",
        tool_calls=[],
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        model_requests=1,
        cost_usd=0.00001,
        provider_evidence=[{"usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001}}],
        provider_cost_usd=0.00001,
        cost_source="provider_response",
        routing_observed=True,
        routing_valid=True,
    )
    outcome.finalize(task, before, after)

    assert outcome.state_available_after is True
    assert outcome.state_contract_passed is False
    assert any(check.operator == "http_status" and not check.passed for check in outcome.state_checks)
    assert outcome.success is False
    assert has_measured_evidence(outcome, task)


def test_registered_skipped_before_observation_is_measured_but_required_null_status_is_not() -> None:
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "table-publication-en")
    before = [
        StateObservation(True, item.resolved_before_expected_status, {})
        if item.check_before
        else StateObservation(True, None, None)
        for item in task.expected_final_state.observation_sets
    ]
    after = [StateObservation(True, item.probe.expected_status, {}) for item in task.expected_final_state.observation_sets]
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="deepseek/deepseek-v4-flash-0731",
        transport="http",
        tool_calls=[],
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        model_requests=1,
        cost_usd=0.00001,
        provider_evidence=[{"usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00001}}],
        provider_cost_usd=0.00001,
        cost_source="provider_response",
        routing_observed=True,
        routing_valid=True,
    )
    outcome.finalize(task, before, after)

    assert has_measured_evidence(outcome, task)
    missing_required_status = outcome.model_copy(deep=True)
    missing_required_status.state_observations_before[0]["status_code"] = None
    assert not has_measured_evidence(missing_required_status, task)
    missing_required_before = outcome.model_copy(deep=True)
    missing_required_before.state_observations_before.pop(0)
    assert not has_measured_evidence(missing_required_before, task)

    forged_skip = outcome.model_copy(deep=True)
    forged_skip.state_observations_before[2]["payload"] = {}
    assert not has_measured_evidence(forged_skip, task)

    missing_after = outcome.model_copy(deep=True)
    missing_after.state_observations_after[0] = {
        "available": False,
        "status_code": None,
        "payload": None,
        "error": "state probe request failed",
    }
    assert not has_measured_evidence(missing_after, task)


def test_expected_json_404_remains_a_passing_state_and_checkpoint_contract() -> None:
    contract = _contract(
        expected_status=404,
        before_expected_status=404,
        must=(StateExpectation(pointer="/detail", operator="equals", value="Vault not found"),),
    )
    observation = StateObservation(True, 404, {"detail": "Vault not found"})

    passed, checks = evaluate_state_contract(contract, observation, observation)
    checkpoint = StateCheckpointContract(
        after_attempt=1,
        probe=StateProbe(service="app", path="/state", expected_status=404),
        must=[StateExpectation(pointer="/detail", operator="equals", value="Vault not found")],
    )

    assert passed is True
    assert all(check.passed for check in checks)
    assert all(check.passed for check in evaluate_checkpoint_contract(checkpoint, observation))

    unexpected = StateObservation(True, 200, {"detail": "Vault not found"})
    assert not evaluate_checkpoint_contract(checkpoint, unexpected)[0].passed
    bad_before = StateObservation(True, 200, {"detail": "Vault not found"})
    assert not evaluate_state_contract(contract, bad_before, observation)[0]


def test_evidence_drops_secret_fields_and_redacts_values(tmp_path: Path) -> None:
    marker = "pat-not-for-evidence"
    value = {"api" + "_key": marker, "message": f"Bearer {marker}", "nested": [marker]}

    redacted = safe_json(value, (marker,))
    path = tmp_path / "evidence.json"
    write_json(path, value, (marker,))

    assert redacted["api" + "_key"] == "[redacted]"
    assert safe_json({"api" + "_key_env": "MCP_BENCH_API_KEY"}, (marker,))["api" + "_key_env"] == "MCP_BENCH_API_KEY"
    assert marker not in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["message"] == "Bearer [redacted]"


def test_evidence_redaction_failure_does_not_persist_the_unredacted_file(tmp_path: Path, monkeypatch) -> None:
    marker = "fixture-marker"
    path = tmp_path / "failed-evidence.json"
    monkeypatch.setattr(evidence, "safe_json", lambda value, _secrets=(): value)

    with pytest.raises(RuntimeError, match="redaction failed"):
        evidence.write_json(path, {"value": marker}, (marker,))

    assert not path.exists()


def test_exception_evidence_keeps_redacted_nested_cause_and_status() -> None:
    marker = "fixture-marker"

    class StatusError(RuntimeError):
        status_code = 413

    cause = StatusError(f"request body rejected: {marker}")
    error = RuntimeError("Connection error.")
    error.__cause__ = cause

    detail = redact_exception(error, (marker,))

    assert "RuntimeError: Connection error." in detail
    assert "StatusError: request body rejected: [redacted]" in detail
    assert "status=413" in detail
    assert marker not in detail
