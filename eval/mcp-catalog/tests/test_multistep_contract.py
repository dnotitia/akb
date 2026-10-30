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
URI = f"akb://{VAULT}/coll/{COLLECTION}/doc/quick-update.md"


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
        server_discover={
            "supportedVersions": ["2026-07-28"],
            "capabilities": {},
            "instructions": "Synthetic server instructions.",
        },
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
    stage_id: str | None = None,
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
        operation_kind="material",
        stage_id=stage_id,
    )


def _exact_calls() -> list[ToolCallRecord]:
    return [
        _call(1, "akb_create_vault", "create", "vault", {"name": VAULT}, stage_id="create_vault_1"),
        _call(2, "akb_create_collection", "create", "collection", {"vault": VAULT, "path": COLLECTION}, stage_id="create_collection_1"),
        _call(
            3,
            "akb_put",
            "create",
            "document",
            {"vault": VAULT, "collection": COLLECTION, "title": TITLE, "content": CONTENT},
            result_fields={"uri": URI},
            stage_id="create_document_1",
        ),
        _call(4, "akb_get", "read", "document", {"uri": URI}, stage_id="read_document_1"),
        _call(5, "akb_provenance", "read", "document", {"uri": URI}, stage_id="read_document_2"),
    ]


def _score(
    task,
    calls: list[ToolCallRecord],
    *,
    before: StateObservation | None = None,
) -> TrialOutcome:
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
    before = before or StateObservation(
        True,
        task.expected_final_state.resolved_before_expected_status,
        {"code": "not_found", "detail": f"Vault not found: {VAULT}"},
    )
    after = StateObservation(
        True,
        200,
        {
            "items": [
                {
                    "name": TITLE,
                    "path": f"{COLLECTION}/quick-update.md",
                    "type": "document",
                    "uri": URI,
                }
            ]
        },
    )
    outcome.finalize(task, before, after)
    return outcome


def test_multistep_pair_declares_semantic_sequence_without_legacy_tool_lock_in() -> None:
    manifest, tasks = _multistep_tasks()
    all_tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")

    assert len(tasks) == 2
    assert {task.locale for task in tasks} == {"ko-KR", "en-US"}
    assert len(all_tasks) == 40
    assert len(manifest.pair_categories) == 20
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


def test_wrong_before_status_prevents_multistep_task_success() -> None:
    _manifest, tasks = _multistep_tasks()
    task = tasks[0]
    outcome = _score(task, _exact_calls(), before=StateObservation(True, 200, {"items": []}))

    assert task.expected_final_state.resolved_before_expected_status == 404
    assert outcome.state_contract_passed is False
    assert outcome.user_outcome_completed is False
    assert outcome.success is False


@pytest.mark.parametrize(
    "mutation",
    ("missing_step", "wrong_order", "wrong_target", "wrong_resource", "missing_binding", "failed_step"),
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
    else:
        calls[1] = calls[1].model_copy(update={"server_succeeded": False, "server_error_code": "internal_error"})

    calls = [call.model_copy(update={"order": order}) for order, call in enumerate(calls, 1)]
    outcome = _score(tasks[0], calls)

    assert outcome.user_outcome_completed is False
    assert outcome.success is False


def test_registered_read_after_multistep_completion_is_reported_without_failing_task() -> None:
    _manifest, tasks = _multistep_tasks()
    calls = [*_exact_calls(), _call(6, "akb_get", "read", "document", {"uri": URI})]

    outcome = _score(tasks[0], calls)

    assert outcome.user_outcome_completed is True
    assert outcome.success is True
    assert outcome.stopping_accuracy is True
    assert outcome.post_completion_overshoot == 1


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


def test_diff_commit_must_appear_in_same_document_history() -> None:
    from mcp_catalog.contracts import ExpectedResultBinding
    from mcp_catalog.execution import _material_result_binding_matches

    binding = ExpectedResultBinding(source_stage="read_document_1", source_field="history", target_stage="read_document_2", target_argument="commit", relation="history_commit")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "history-provenance-en")
    calls = [
        _call(1, "akb_history", "read", "document", {"uri": URI}, result_fields={"history": '[{"hash":"abcdef0"}]'}, stage_id="read_document_1"),
        _call(2, "akb_diff", "read", "document", {"uri": URI, "commit": "abcdef0"}, stage_id="read_document_2"),
    ]
    assert _material_result_binding_matches(binding, task, calls)
    calls[1].server_args["commit"] = "deadbeef"
    calls[1].effective_server_args["commit"] = "deadbeef"
    assert not _material_result_binding_matches(binding, task, calls)


def test_candidate_history_modes_match_only_their_registered_stages() -> None:
    from mcp_catalog.contracts import ArmOperationRoute
    from mcp_catalog.execution import ToolCallRecorder, _public_route

    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "history-provenance-en")
    routes = [item.model_copy(deep=True) for item in manifest.public_operations]
    for item in routes:
        if item.operation in {"akb_history", "akb_diff"}:
            item.candidate = ArmOperationRoute(tool="akb_history", mode="list" if item.operation == "akb_history" else "diff")
    recorder = ToolCallRecorder(operation_map=manifest.operation_map, secrets=(), task=task)
    recorder.next_expected_stage = 3
    uri = "akb://catalog-bench-history/doc/history-note.md"
    route = _public_route("akb_history", {"uri": uri}, routes, arm="candidate", transport="http")
    assert route is not None
    assert recorder._stage_id_for_material_call("akb_history", "read", "document", {"uri": uri}, canonical_operation=route.operation) == "read_document_1"
    route = _public_route("akb_history", {"uri": uri, "mode": "diff", "commit": "abcdef0"}, routes, arm="candidate", transport="http")
    assert route is not None
    assert recorder._stage_id_for_material_call("akb_history", "read", "document", {"uri": uri, "mode": "diff", "commit": "abcdef0"}, canonical_operation=route.operation) == "read_document_2"


def test_foreign_commit_control_accepts_only_empty_diff_or_explicit_rejection() -> None:
    from mcp_catalog.contracts import ExpectedMaterialAttempt
    from mcp_catalog.execution import _material_attempt_matches

    expected = ExpectedMaterialAttempt(stage_id="read_document_1", logical_operation="read", tool_name="akb_diff", resource_type="document", arguments={"uri": URI}, outcome="no_document_diff")
    call = _call(1, "akb_diff", "read", "document", {"uri": URI, "commit": "abcdef0"}, result_fields={"type": "unchanged", "diff": ""}, stage_id="read_document_1")
    assert _material_attempt_matches(call, expected, dynamic_arguments={"commit"}, consumer_root=None)
    call.result_fields["diff"] = "+leak"
    assert not _material_attempt_matches(call, expected, dynamic_arguments={"commit"}, consumer_root=None)
    call.server_succeeded = False
    call.server_error_code = "invalid_argument"
    assert _material_attempt_matches(call, expected, dynamic_arguments={"commit"}, consumer_root=None)


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
def test_history_boundary_control_binds_foreign_revision_and_target(arm) -> None:
    from mcp_catalog.execution import material_outcome_matches

    task = next(task for task in load_task_corpus(ROOT / "corpus" / "history-tasks.json") if task.id == "history-boundaries-en")
    first = "akb://catalog-bench-history-boundary/doc/first.md"
    second = "akb://catalog-bench-history-boundary/doc/second.md"
    diff_tool = "akb_diff" if arm == "baseline" else "akb_history"
    diff_mode = {} if arm == "baseline" else {"mode": "diff"}
    calls = [
        _call(1, "akb_create_vault", "create", "vault", {"name": "catalog-bench-history-boundary"}, stage_id="create_vault_1"),
        _call(2, "akb_put", "create", "document", {"vault": "catalog-bench-history-boundary", "title": "first", "content": "first body"}, result_fields={"uri": first}, stage_id="create_document_1"),
        _call(3, "akb_put", "create", "document", {"vault": "catalog-bench-history-boundary", "title": "second", "content": "second body"}, result_fields={"uri": second}, stage_id="create_document_2"),
        _call(4, "akb_history", "read", "document", {"uri": second}, result_fields={"history": '[{"hash":"abcdef0"}]'}, stage_id="read_document_1"),
        _call(5, diff_tool, "read", "document", {"uri": first, "commit": "abcdef0", **diff_mode}, result_fields={"type": "unchanged", "diff": ""}, stage_id="read_document_2"),
        _call(6, diff_tool, "read", "document", {"uri": first, "commit": "not-a-hash", **diff_mode}, succeeded=False, stage_id="read_document_3"),
    ]
    calls[5].server_error_code = "invalid_argument"
    calls[5].server_status_code = None
    calls[4].canonical_operation = calls[5].canonical_operation = "akb_diff"
    if arm == "candidate":
        calls[4].server_succeeded = False
        calls[4].server_error_code = "invalid_argument"
    assert material_outcome_matches(task, calls)[0]
    calls[4].server_args["uri"] = second
    calls[4].effective_server_args["uri"] = second
    assert not material_outcome_matches(task, calls)[0]


def test_history_mode_and_commit_binding_errors_are_reported_separately() -> None:
    from mcp_catalog.execution import _trial_error_kinds

    task = next(task for task in load_task_corpus(ROOT / "corpus" / "history-tasks.json") if task.id == "history-provenance-en")
    call = _call(1, "akb_history", "unknown", "unknown", {"uri": URI, "mode": "other"})
    outcome = TrialOutcome(task_id=task.id, category=task.category, locale=task.locale, arm="candidate", model_class="primary", model_id="model", transport="http", tool_calls=[call])
    outcome.result_binding_accuracy = False
    errors = _trial_error_kinds(outcome, task)
    assert "wrong_mode" in errors and "result_binding_error" in errors


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
def test_history_provenance_requires_returned_commit_and_correct_document(arm) -> None:
    from mcp_catalog.execution import material_outcome_matches

    task = next(task for task in load_task_corpus(ROOT / "corpus" / "history-tasks.json") if task.id == "history-provenance-en")
    uri = "akb://catalog-bench-history/doc/history-note.md"
    tool = "akb_diff" if arm == "baseline" else "akb_history"
    mode = {} if arm == "baseline" else {"mode": "diff"}
    calls = [
        _call(1, "akb_create_vault", "create", "vault", {"name": "catalog-bench-history"}, stage_id="create_vault_1"),
        _call(2, "akb_put", "create", "document", {"vault": "catalog-bench-history", "title": "history-note", "content": "current: one"}, result_fields={"uri": uri}, stage_id="create_document_1"),
        _call(3, "akb_update", "update", "document", {"uri": uri, "content": "current: two"}, stage_id="update_document_1"),
        _call(4, "akb_history", "read", "document", {"uri": uri}, result_fields={"history": '[{"hash":"abcdef0"}]'}, stage_id="read_document_1"),
        _call(5, tool, "read", "document", {"uri": uri, "commit": "abcdef0", **mode}, stage_id="read_document_2"),
        _call(6, "akb_provenance", "read", "document", {"uri": uri}, stage_id="read_document_3"),
        _call(7, "akb_get", "read", "document", {"uri": uri}, stage_id="read_document_4"),
    ]
    calls[4].canonical_operation = "akb_diff"
    assert material_outcome_matches(task, calls)[0]
    calls[4].server_args["commit"] = "deadbeef"
    calls[4].effective_server_args["commit"] = "deadbeef"
    assert not material_outcome_matches(task, calls)[0]
    calls[4].server_args["commit"] = calls[4].effective_server_args["commit"] = "abcdef0"
    calls[3].server_args["uri"] = calls[3].effective_server_args["uri"] = "akb://wrong/doc/other.md"
    assert not material_outcome_matches(task, calls)[0]
