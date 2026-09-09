from __future__ import annotations

import json
from pathlib import Path

from mcp_catalog.contracts import StateContract, StateExpectation, StateProbe
from mcp_catalog.evidence import safe_json, write_json
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
