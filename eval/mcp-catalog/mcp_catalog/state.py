"""Deterministic final-state assertions used by the benchmark scorer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]

from .contracts import StateCheckpointContract, StateContract, StateExpectation, StateExpectationSet
from .runtime import StateObservation

MISSING = object()
_OKF_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


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
    if expectation.operator == "nonempty":
        return isinstance(actual, str) and bool(actual.strip())
    if expectation.operator == "okf_document":
        return _matches_okf_document(actual, expectation.value)
    if expectation.operator == "equals":
        return actual == expectation.value
    if expectation.operator == "contains":
        return recursively_contains(actual, expectation.value)
    if expectation.operator == "not_contains":
        return not recursively_contains(actual, expectation.value)
    raise AssertionError(f"unknown state operator: {expectation.operator}")


def _matches_okf_document(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, str) or not isinstance(expected, dict):
        return False
    match = _OKF_FRONTMATTER.match(actual)
    if match is None:
        return False
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return False
    if not isinstance(metadata, dict):
        return False
    resource_uri = expected.get("resource_uri")
    if (
        metadata.get("type") != expected.get("type")
        or metadata.get("resource") != resource_uri
        or metadata.get("akb_uri") != resource_uri
    ):
        return False
    body = actual[match.end():].replace("\r\n", "\n")
    if not body.startswith("\n") or not body.endswith("\n"):
        return False
    return body[1:-1] == expected.get("body")


def evaluate_state_contract(
    contract: StateContract,
    before: StateObservation | list[StateObservation],
    after: StateObservation | list[StateObservation],
) -> tuple[bool, list[StateCheckResult]]:
    checks: list[StateCheckResult] = []
    before_items = before if isinstance(before, list) else [before]
    after_items = after if isinstance(after, list) else [after]
    observations = contract.observation_sets
    if len(after_items) != len(observations) or len(before_items) != len(observations):
        checks.append(StateCheckResult(False, "", "available", "state observation count does not match contract"))
        return False, checks
    for expectation_set, before_item, after_item in zip(observations, before_items, after_items, strict=True):
        checks.extend(_evaluate_expectations(expectation_set, before_item, after_item))
    return all(check.passed for check in checks), checks


def _evaluate_expectations(
    contract: StateExpectationSet,
    before: StateObservation,
    after: StateObservation,
) -> list[StateCheckResult]:
    checks: list[StateCheckResult] = []
    if not after.available:
        return [StateCheckResult(False, "", "available", after.error or "after-state observation unavailable")]
    for expectation in contract.must:
        actual = json_pointer(after.payload, expectation.pointer)
        checks.append(_result(expectation, expectation_holds(actual, expectation), actual, "must"))
    for expectation in contract.must_not:
        actual = json_pointer(after.payload, expectation.pointer)
        checks.append(_result(expectation, not expectation_holds(actual, expectation), actual, "must_not"))
    if contract.check_before:
        if not before.available:
            checks.append(StateCheckResult(False, "", "available_before", before.error or "before-state observation unavailable"))
        for expectation in contract.before_must:
            actual = json_pointer(before.payload, expectation.pointer)
            checks.append(_result(expectation, expectation_holds(actual, expectation), actual, "before_must"))
        for expectation in contract.before_must_not:
            actual = json_pointer(before.payload, expectation.pointer)
            checks.append(_result(expectation, not expectation_holds(actual, expectation), actual, "before_must_not"))
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
    return checks


def evaluate_checkpoint_contract(
    contract: StateCheckpointContract,
    observation: StateObservation,
) -> list[StateCheckResult]:
    if not observation.available:
        return [
            StateCheckResult(
                False,
                "",
                f"checkpoint:{contract.after_attempt}:available",
                observation.error or "checkpoint state observation unavailable",
            )
        ]
    checks: list[StateCheckResult] = []
    for expectation in contract.must:
        actual = json_pointer(observation.payload, expectation.pointer)
        checks.append(_result(expectation, expectation_holds(actual, expectation), actual, "checkpoint_must"))
    for expectation in contract.must_not:
        actual = json_pointer(observation.payload, expectation.pointer)
        checks.append(_result(expectation, not expectation_holds(actual, expectation), actual, "checkpoint_must_not"))
    if not checks:
        checks.append(
            StateCheckResult(False, "", f"checkpoint:{contract.after_attempt}:empty", "checkpoint has no state expectations")
        )
    return checks


def _result(expectation: StateExpectation, passed: bool, actual: Any, phase: str) -> StateCheckResult:
    actual_text = "missing" if actual is MISSING else repr(actual)[:240]
    return StateCheckResult(
        passed,
        expectation.pointer,
        f"{phase}:{expectation.operator}",
        f"expected {expectation.value!r}; actual {actual_text}",
    )
