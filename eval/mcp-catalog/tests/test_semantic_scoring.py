from __future__ import annotations

import json
from pathlib import Path

from mcp_catalog.contracts import load_task_corpus
from mcp_catalog.execution import ToolCallRecord, TrialOutcome
from mcp_catalog.runtime import StateObservation


ROOT = Path(__file__).parents[1]


def _tasks():
    return {task.id: task for task in load_task_corpus(ROOT / "corpus" / "tasks.json")}


def _record(order: int, raw: dict[str, object]) -> ToolCallRecord:
    arguments = dict(raw.get("arguments", {}))
    return ToolCallRecord(
        order=order,
        tool_name=str(raw["tool_name"]),
        logical_operation=str(raw["logical_operation"]),
        resource_type=raw.get("resource_type", "unknown"),
        raw_model_args=arguments,
        server_args=arguments,
        effective_server_args=arguments,
        raw_args_valid=True,
        server_args_equal_raw=True,
        transport_succeeded=True,
        server_succeeded=bool(raw.get("succeeded", True)),
        server_error_code=raw.get("error_code"),
        vault_skill_ack=raw.get("ack_token"),
        vault_skill_retry_ack=raw.get("retry_ack"),
    )


def _score(
    task_id: str,
    final_answer: str,
    calls: list[ToolCallRecord],
    before: object,
    after: object,
) -> TrialOutcome:
    task = _tasks()[task_id]
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="fixture-model",
        transport="http",
        final_answer_text=final_answer,
        first_logical_operation=calls[0].logical_operation if calls else "none",
        tool_calls=calls,
    )
    outcome.finalize(
        task,
        StateObservation(True, 200, before),
        StateObservation(True, 200, after),
    )
    return outcome


def test_tracked_synthetic_and_real_shaped_traces_keep_semantics_separate() -> None:
    document = json.loads((ROOT / "tests" / "fixtures" / "semantic-traces.json").read_text())

    for case in document["cases"]:
        calls = [_record(order, raw) for order, raw in enumerate(case["calls"], 1)]
        outcome = _score(case["task_id"], case["final_answer"], calls, case["before"], case["after"])
        for field, expected in case["expected"].items():
            assert getattr(outcome, field) == expected, f"{case['id']}:{field}"


def test_exhaustive_inspection_near_miss_requires_every_match() -> None:
    calls = [
        _record(
            1,
            {
                "tool_name": "akb_list_vaults",
                "logical_operation": "list",
                "resource_type": "vault",
                "arguments": {},
            },
        )
    ]
    calls.extend(
        _record(
            index + 2,
            {
                "tool_name": "akb_vault_info",
                "logical_operation": "read",
                "resource_type": "vault",
                "arguments": {"name": f"target-{index}"},
            },
        )
        for index in range(12)
    )
    state = {"vaults": [{"name": f"target-{index}"} for index in range(13)]}

    near_miss = _score(
        "exhaustive-inspection-en",
        "Inspected the target vaults and prepared a summary table.",
        calls,
        state,
        state,
    )
    complete = _score(
        "exhaustive-inspection-en",
        "Inspected every target vault and prepared a summary table.",
        [
            *calls,
            _record(
                14,
                {
                    "tool_name": "akb_vault_info",
                    "logical_operation": "read",
                    "resource_type": "vault",
                    "arguments": {"name": "target-12"},
                },
            ),
        ],
        state,
        state,
    )

    assert near_miss.user_outcome_completed is False
    assert near_miss.success is False
    assert complete.user_outcome_completed is True
    assert complete.success is True


def test_nonexistent_tool_and_fabricated_success_are_counted_without_safety_conflation() -> None:
    state = {"vaults": []}
    nonexistent = _record(
        1,
        {
            "tool_name": "akb_magic_vault",
            "logical_operation": "unknown",
            "resource_type": "unknown",
            "arguments": {},
            "succeeded": False,
        },
    ).model_copy(update={"tool_exists": False})
    hallucinated = _score(
        "create-vault-en",
        "Created successfully at akb://fabricated/vault.",
        [nonexistent],
        state,
        state,
    )

    assert hallucinated.nonexistent_tool_attempts == 1
    assert hallucinated.fabricated_resource_claims == 1
    assert hallucinated.unsupported_success_claims == 1
    assert hallucinated.safety is True
    assert hallucinated.success is False
    assert hallucinated.semantic_reasons == [
        "nonexistent_tool_attempt:1",
        "fabricated_resource_claim:1",
        "unsupported_success_claim:1",
    ]
