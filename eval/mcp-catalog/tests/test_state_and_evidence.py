from __future__ import annotations

import json
from pathlib import Path

import pytest

import mcp_catalog.evidence as evidence
from mcp_catalog.contracts import StateContract, StateExpectation, StateProbe
from mcp_catalog.evidence import redact_exception, safe_json, write_json
from mcp_catalog.runtime import StateObservation
from mcp_catalog.state import evaluate_state_contract


def _contract(*, must=(), must_not=(), unchanged=()) -> StateContract:
    return StateContract(
        probe=StateProbe(service="app", path="/state"),
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
