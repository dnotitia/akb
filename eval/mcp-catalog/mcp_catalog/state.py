"""Deterministic final-state assertions used by the benchmark scorer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import StateContract, StateExpectation
from .runtime import StateObservation

MISSING = object()


@dataclass(frozen=True, slots=True)
class StateCheckResult:
    passed: bool
    pointer: str
    operator: str
    reason: str


def json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    current = value
    for raw_part in pointer.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current


def recursively_contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list) and not isinstance(expected, list):
        return any(recursively_contains(item, expected) for item in actual)
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and recursively_contains(actual[key], item) for key, item in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(recursively_contains(item, wanted) for item in actual) for wanted in expected)
    return actual == expected


def expectation_holds(actual: Any, expectation: StateExpectation) -> bool:
    if expectation.operator == "exists":
        return (actual is not MISSING) is bool(expectation.value)
    if actual is MISSING:
        return False
    if expectation.operator == "equals":
        return actual == expectation.value
    if expectation.operator == "contains":
        return recursively_contains(actual, expectation.value)
    if expectation.operator == "not_contains":
        return not recursively_contains(actual, expectation.value)
    raise AssertionError(f"unknown state operator: {expectation.operator}")


def evaluate_state_contract(
    contract: StateContract,
    before: StateObservation,
    after: StateObservation,
) -> tuple[bool, list[StateCheckResult]]:
    checks: list[StateCheckResult] = []
    if not after.available:
        checks.append(
            StateCheckResult(False, "", "available", after.error or "after-state observation unavailable")
        )
        return False, checks
    if contract.must or contract.must_not or contract.unchanged:
        for expectation in contract.must:
            actual = json_pointer(after.payload, expectation.pointer)
            passed = expectation_holds(actual, expectation)
            checks.append(_result(expectation, passed, actual, "must"))
        for expectation in contract.must_not:
            actual = json_pointer(after.payload, expectation.pointer)
            passed = not expectation_holds(actual, expectation)
            checks.append(_result(expectation, passed, actual, "must_not"))
        for pointer in contract.unchanged:
            before_value = json_pointer(before.payload, pointer) if before.available else MISSING
            after_value = json_pointer(after.payload, pointer)
            passed = before.available and before_value == after_value
            checks.append(
                StateCheckResult(
                    passed,
                    pointer,
                    "unchanged",
                    "unchanged" if passed else "value changed or before-state unavailable",
                )
            )
    return all(check.passed for check in checks), checks


def _result(expectation: StateExpectation, passed: bool, actual: Any, phase: str) -> StateCheckResult:
    actual_text = "missing" if actual is MISSING else repr(actual)[:240]
    return StateCheckResult(
        passed,
        expectation.pointer,
        f"{phase}:{expectation.operator}",
        f"expected {expectation.value!r}; actual {actual_text}",
    )
