from __future__ import annotations

from pathlib import Path

import pytest
from mcp_types import Tool

from mcp_catalog.catalog import input_schemas_from_catalog
from mcp_catalog.contracts import (
    CatalogSnapshot,
    hash_json,
    load_run_manifest,
    load_task_corpus,
    source_blind_violations_for,
    token_estimate,
)
from mcp_catalog.execution import (
    ToolCallRecord,
    TrialOutcome,
    canonicalize_arguments,
    capture_tool_input_schemas,
)
from mcp_catalog.runtime import StateObservation


ROOT = Path(__file__).parents[1]
VAULT = "catalog-bench-knowledge"
COLLECTION = "notes"
TITLE = "quick-update"
CONTENT = "A short note for the team."
URI = f"akb://{VAULT}/coll/{COLLECTION}/doc/quick-update"


class _ListedToolset:
    async def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="akb_create_vault",
                input_schema={"properties": {"public_access": {"default": "none"}}},
            ),
            Tool(
                name="akb_put_file",
                input_schema={"properties": {"collection": {"default": ""}}},
            ),
            Tool(
                name="akb_put",
                input_schema={
                    "properties": {"type": {"default": "note"}, "status": {"default": "draft"}}
                },
            ),
        ]


def _multistep_tasks():
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    manifest.validate_tasks(tasks)
    return manifest, [task for task in tasks if task.pair_id == "knowledge-workflow"]


@pytest.mark.asyncio
async def test_raw_server_tool_schemas_drive_server_argument_defaults() -> None:
    schemas = await capture_tool_input_schemas(_ListedToolset())

    assert canonicalize_arguments({"name": VAULT}, schemas["akb_create_vault"]) == {
        "name": VAULT,
        "public_access": "none",
    }
    assert canonicalize_arguments(
        {"parent": f"akb://{VAULT}", "file_path": "sample-note.txt"},
        schemas["akb_put_file"],
    ) == {"parent": f"akb://{VAULT}", "file_path": "sample-note.txt", "collection": ""}


def test_captured_catalog_schema_is_the_scorer_authority() -> None:
    tools = [
        {
            "name": "akb_put",
            "inputSchema": {
                "type": "object",
                "properties": {"type": {"default": "note"}, "status": {"default": "draft"}},
            },
        }
    ]
    snapshot = CatalogSnapshot(
        transport="http",
        source_revision="a" * 40,
        artifact_version="0.0.0",
        tool_count=1,
        catalog_hash=hash_json(tools),
        catalog_token_estimate=token_estimate(tools),
        tools=tools,
    )

    assert input_schemas_from_catalog(snapshot) == {"akb_put": tools[0]["inputSchema"]}


def _call(
    order: int,
    tool_name: str,
    logical_operation: str,
    resource_type: str,
    arguments: dict[str, object],
    *,
    result_fields: dict[str, str] | None = None,
    succeeded: bool = True,
) -> ToolCallRecord:
    return ToolCallRecord(
        order=order,
        tool_name=tool_name,
        logical_operation=logical_operation,
        resource_type=resource_type,
        raw_model_args=arguments,
        server_args=arguments,
        effective_server_args=arguments,
        raw_args_valid=True,
        server_args_equal_raw=True,
        transport_succeeded=True,
        server_succeeded=succeeded,
        server_status_code=None if succeeded else 500,
        server_error_code=None if succeeded else "internal_error",
        result_fields=result_fields or {},
    )


def _exact_calls() -> list[ToolCallRecord]:
    return [
        _call(1, "akb_create_vault", "create", "vault", {"name": VAULT}),
        _call(2, "akb_create_collection", "create", "collection", {"vault": VAULT, "path": COLLECTION}),
        _call(
            3,
            "akb_put",
            "create",
            "document",
            {"vault": VAULT, "collection": COLLECTION, "title": TITLE, "content": CONTENT},
            result_fields={"uri": URI},
        ),
        _call(4, "akb_get", "read", "document", {"uri": URI}),
        _call(5, "akb_provenance", "read", "document", {"uri": URI}),
    ]


def _score(task, calls: list[ToolCallRecord]) -> TrialOutcome:
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="model",
        transport="http",
        final_answer_text="quick-update was created and confirmed.",
        first_logical_operation=calls[0].logical_operation if calls else "none",
        tool_calls=calls,
    )
    before = StateObservation(True, 200, {"items": []})
    after = StateObservation(True, 200, {"items": [{"name": "quick-update.md"}]})
    outcome.finalize(task, before, after)
    return outcome


def test_multistep_pair_declares_semantic_sequence_without_legacy_tool_lock_in() -> None:
    manifest, tasks = _multistep_tasks()
    all_tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")

    assert len(tasks) == 2
    assert {task.locale for task in tasks} == {"ko-KR", "en-US"}
    assert len(all_tasks) == 26
    assert len(manifest.pair_categories) == 13
    assert source_blind_violations_for(tasks, manifest.operation_map) == []
    for task in tasks:
        assert [attempt.logical_operation for attempt in task.expected_material_attempts] == [
            "create",
            "create",
            "create",
            "read",
            "read",
        ]
        assert [attempt.resource_type for attempt in task.expected_material_attempts] == [
            "vault",
            "collection",
            "document",
            "document",
            "document",
        ]
        assert all(attempt.tool_name is None for attempt in task.expected_material_attempts)
        assert len(task.expected_result_bindings) == 2


def test_exact_outcome_and_cross_call_dataflow_complete_the_multistep_task() -> None:
    _manifest, tasks = _multistep_tasks()
    outcome = _score(tasks[0], _exact_calls())

    assert outcome.required_attempts_completed is True
    assert outcome.multi_step_ordering is True
    assert outcome.result_binding_accuracy is True
    assert outcome.user_outcome_completed is True
    assert outcome.success is True


@pytest.mark.parametrize(
    "mutation",
    ("missing_step", "wrong_order", "wrong_target", "wrong_resource", "missing_binding", "failed_step", "extra_step"),
)
def test_incomplete_or_semantically_wrong_multistep_trace_fails(mutation: str) -> None:
    _manifest, tasks = _multistep_tasks()
    calls = _exact_calls()

    if mutation == "missing_step":
        calls = calls[:-1]
    elif mutation == "wrong_order":
        calls[0], calls[1] = calls[1], calls[0]
    elif mutation == "wrong_target":
        calls[1] = calls[1].model_copy(
            update={"server_args": {"vault": "other", "path": COLLECTION}, "effective_server_args": {"vault": "other", "path": COLLECTION}}
        )
    elif mutation == "wrong_resource":
        calls[1] = calls[1].model_copy(update={"resource_type": "document"})
    elif mutation == "missing_binding":
        calls[2] = calls[2].model_copy(update={"result_fields": {}})
    elif mutation == "failed_step":
        calls[1] = calls[1].model_copy(update={"server_succeeded": False, "server_error_code": "internal_error"})
    elif mutation == "extra_step":
        calls.append(_call(6, "akb_get", "read", "document", {"uri": URI}))

    calls = [call.model_copy(update={"order": order}) for order, call in enumerate(calls, 1)]
    outcome = _score(tasks[0], calls)

    assert outcome.user_outcome_completed is False
    assert outcome.success is False


def test_equivalent_read_tools_are_accepted_when_semantics_and_binding_match() -> None:
    _manifest, tasks = _multistep_tasks()
    calls = _exact_calls()
    calls[3] = calls[3].model_copy(update={"tool_name": "akb_drill_down"})

    outcome = _score(tasks[0], calls)

    assert outcome.tool_outcome_match is True
    assert outcome.success is True


def test_manifest_rejects_unknown_semantic_operation() -> None:
    manifest, tasks = _multistep_tasks()
    changed_by_id = {
        task.id: task.model_copy(update={"allowed_material_operations": [*task.allowed_material_operations, "invent"]})
        for task in tasks
    }

    with pytest.raises(ValueError, match="operation_map"):
        all_tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
        manifest.validate_tasks([changed_by_id.get(task.id, task) for task in all_tasks])
