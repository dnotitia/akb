from __future__ import annotations

from pathlib import Path

import pytest
from mcp_types import Tool

from mcp_catalog.contracts import CatalogSnapshot, hash_json, load_run_manifest, load_task_corpus, source_blind_violations_for, token_estimate
from mcp_catalog.execution import (
    ToolCallRecord,
    TrialOutcome,
    canonicalize_arguments,
    capture_tool_input_schemas,
)
from mcp_catalog.catalog import input_schemas_from_catalog
from mcp_catalog.runtime import StateObservation


ROOT = Path(__file__).parents[1]
VAULT = "catalog-bench-multi"
COLLECTION = "team-notes"
TITLE = "quick-update"
CONTENT = "A short note for the team."
EXPECTED_ATTEMPTS = [
    ("akb_create_vault", {"name": VAULT, "public_access": "none"}),
    ("akb_create_collection", {"vault": VAULT, "path": COLLECTION}),
    (
        "akb_put",
        {
            "vault": VAULT,
            "collection": COLLECTION,
            "title": TITLE,
            "content": CONTENT,
            "type": "note",
            "status": "draft",
        },
    ),
]
REQUIRED_ATTEMPTS = [
    ("akb_create_vault", {"name": VAULT}),
    ("akb_create_collection", {"vault": VAULT, "path": COLLECTION}),
    ("akb_put", {"vault": VAULT, "collection": COLLECTION, "title": TITLE, "content": CONTENT}),
]
RAW_ATTEMPTS = [
    ("akb_create_vault", {"name": VAULT}),
    ("akb_create_collection", {"vault": VAULT, "path": COLLECTION}),
    ("akb_put", {"vault": VAULT, "collection": COLLECTION, "title": TITLE, "content": CONTENT}),
]
PUBLIC_INPUT_SCHEMAS = {
    "akb_create_vault": {"properties": {"public_access": {"default": "none"}}},
    "akb_put": {
        "properties": {
            "type": {"default": "note"},
            "status": {"default": "draft"},
        }
    },
}


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
    return manifest, [task for task in tasks if task.pair_id == "multi-step"]


@pytest.mark.asyncio
async def test_raw_server_tool_schemas_drive_server_argument_defaults() -> None:
    schemas = await capture_tool_input_schemas(_ListedToolset())

    assert canonicalize_arguments(
        {"name": VAULT}, schemas["akb_create_vault"]
    ) == {"name": VAULT, "public_access": "none"}
    assert canonicalize_arguments(
        {"parent": "akb://catalog-bench-multi", "file_path": "sample-note.txt"},
        schemas["akb_put_file"],
    ) == {"parent": "akb://catalog-bench-multi", "file_path": "sample-note.txt", "collection": ""}
    assert canonicalize_arguments(
        {"vault": VAULT, "collection": COLLECTION, "title": TITLE, "content": CONTENT},
        schemas["akb_put"],
    ) == {
        "vault": VAULT,
        "collection": COLLECTION,
        "title": TITLE,
        "content": CONTENT,
        "type": "note",
        "status": "draft",
    }


def test_captured_catalog_schema_is_the_scorer_authority() -> None:
    tools = [
        {
            "name": "akb_put",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "type": {"default": "note"},
                    "status": {"default": "draft"},
                },
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

    assert input_schemas_from_catalog(snapshot) == {
        "akb_put": tools[0]["inputSchema"],
    }


def _call(
    order: int,
    tool_name: str,
    arguments: dict[str, object],
    *,
    succeeded: bool = True,
) -> ToolCallRecord:
    return ToolCallRecord(
        order=order,
        tool_name=tool_name,
        logical_operation="create",
        raw_model_args=arguments,
        server_args=arguments,
        effective_server_args=canonicalize_arguments(arguments, PUBLIC_INPUT_SCHEMAS.get(tool_name, {})),
        raw_args_valid=True,
        server_args_equal_raw=True,
        transport_succeeded=True,
        server_succeeded=succeeded,
        server_status_code=None if succeeded else 500,
        server_error_code=None if succeeded else "internal_error",
    )


def _exact_calls() -> list[ToolCallRecord]:
    return [
        _call(order, tool_name, arguments) for order, (tool_name, arguments) in enumerate(RAW_ATTEMPTS, start=1)
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
        final_answer_text="The vault, collection, and document are ready.",
        first_logical_operation="create",
        tool_calls=calls,
    )
    before = StateObservation(True, 200, {"vaults": []})
    after = StateObservation(True, 200, {"vaults": [{"name": VAULT}]})
    outcome.finalize(task, before, after)
    return outcome


def test_multistep_pair_declares_the_same_exact_ordered_public_attempts() -> None:
    manifest, tasks = _multistep_tasks()
    all_tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")

    assert len(tasks) == 2
    assert {task.locale for task in tasks} == {"ko-KR", "en-US"}
    assert len(all_tasks) == 16
    assert len(manifest.pair_categories) == 8
    assert sum(len(task.fixture.transports) for task in all_tasks) * len(manifest.models) * manifest.repeats == 180
    assert source_blind_violations_for(tasks, manifest.operation_map) == []

    expected = [(tool_name, "create", arguments, "success") for tool_name, arguments in REQUIRED_ATTEMPTS]
    for task in tasks:
        assert task.material_attempt_limits == {"create": 3}
        assert [
            (attempt.tool_name, attempt.logical_operation, attempt.arguments, attempt.outcome)
            for attempt in task.expected_material_attempts
        ] == expected
        assert task.expected_final_state.probe.path == "/api/v1/vaults"
        assert task.expected_final_state.must[0].value == {"name": VAULT}
        for target in (VAULT, COLLECTION, TITLE, CONTENT):
            assert target in task.prompt

    assert [attempt.model_dump(mode="json") for attempt in tasks[0].expected_material_attempts] == [
        attempt.model_dump(mode="json") for attempt in tasks[1].expected_material_attempts
    ]


def test_only_the_exact_three_call_trace_completes_the_multistep_task() -> None:
    _manifest, tasks = _multistep_tasks()
    outcome = _score(tasks[0], _exact_calls())

    assert outcome.required_attempts_completed is True
    assert outcome.tool_outcome_match is True
    assert outcome.success is True
    assert [call.effective_server_args for call in outcome.tool_calls] == [
        arguments for _tool_name, arguments in EXPECTED_ATTEMPTS
    ]


def test_explicit_public_defaults_match_the_same_canonical_attempt_contract() -> None:
    _manifest, tasks = _multistep_tasks()
    calls = [_call(order, tool_name, args) for order, (tool_name, args) in enumerate(EXPECTED_ATTEMPTS, 1)]

    outcome = _score(tasks[0], calls)

    assert outcome.tool_outcome_match is True
    assert outcome.success is True


@pytest.mark.parametrize(
    "mutation",
    (
        "vault_only",
        "missing_collection",
        "missing_document",
        "wrong_order",
        "wrong_tool",
        "wrong_vault",
        "wrong_collection_path",
        "wrong_document_collection",
        "wrong_title",
        "wrong_content",
        "failed_step",
        "extra_create",
    ),
)
def test_incomplete_or_noncanonical_multistep_trace_fails(mutation: str) -> None:
    _manifest, tasks = _multistep_tasks()
    calls = _exact_calls()

    if mutation == "vault_only":
        calls = calls[:1]
    elif mutation == "missing_collection":
        calls = [calls[0], calls[2]]
    elif mutation == "missing_document":
        calls = calls[:2]
    elif mutation == "wrong_order":
        calls = [
            _call(1, "akb_create_collection", {"vault": VAULT, "path": COLLECTION}),
            _call(2, "akb_create_vault", {"name": VAULT}),
            _call(3, "akb_put", {"vault": VAULT, "collection": COLLECTION, "title": TITLE, "content": CONTENT}),
        ]
    elif mutation == "wrong_tool":
        calls[1] = _call(
            2,
            "akb_put",
            {"vault": VAULT, "collection": COLLECTION, "title": TITLE, "content": CONTENT},
        )
    elif mutation == "wrong_vault":
        calls[0] = _call(1, "akb_create_vault", {"name": "other-vault"})
    elif mutation == "wrong_collection_path":
        calls[1] = _call(2, "akb_create_collection", {"vault": VAULT, "path": "other-notes"})
    elif mutation == "wrong_document_collection":
        calls[2] = _call(
            3, "akb_put", {"vault": VAULT, "collection": "other-notes", "title": TITLE, "content": CONTENT}
        )
    elif mutation == "wrong_title":
        calls[2] = _call(
            3, "akb_put", {"vault": VAULT, "collection": COLLECTION, "title": "other-title", "content": CONTENT}
        )
    elif mutation == "wrong_content":
        calls[2] = _call(
            3, "akb_put", {"vault": VAULT, "collection": COLLECTION, "title": TITLE, "content": "Different content."}
        )
    elif mutation == "failed_step":
        calls[1] = _call(2, "akb_create_collection", {"vault": VAULT, "path": COLLECTION}, succeeded=False)
    elif mutation == "extra_create":
        calls.append(_call(4, "akb_create_collection", {"vault": VAULT, "path": "extra"}))

    outcome = _score(tasks[0], calls)

    assert outcome.required_attempts_completed is False
    assert outcome.tool_outcome_match is False
    assert outcome.success is False


def test_public_schema_valid_optional_metadata_does_not_block_multistep_completion() -> None:
    _manifest, tasks = _multistep_tasks()
    calls = _exact_calls()
    calls[0].effective_server_args["public_access"] = "reader"
    calls[2] = _call(
        3,
        "akb_put",
        {
            "parent": f"akb://{VAULT}/coll/{COLLECTION}",
            "title": TITLE,
            "content": CONTENT,
            "status": "active",
            "slug": "quick-update",
        },
    )

    outcome = _score(tasks[0], calls)

    assert outcome.tool_outcome_match is True
    assert outcome.success is True


def test_malformed_parent_uri_is_a_scoring_mismatch() -> None:
    _manifest, tasks = _multistep_tasks()
    calls = _exact_calls()
    calls[2] = _call(
        3,
        "akb_put",
        {"parent": "akb://[", "title": TITLE, "content": CONTENT},
    )

    outcome = _score(tasks[0], calls)

    assert outcome.tool_outcome_match is False
    assert outcome.success is False


def test_manifest_rejects_attempt_tool_not_registered_for_its_logical_operation() -> None:
    manifest, tasks = _multistep_tasks()
    changed_by_id = {}
    for task in tasks:
        attempts = [
            attempt.model_copy(update={"tool_name": "akb_search"}) if index == 1 else attempt
            for index, attempt in enumerate(task.expected_material_attempts)
        ]
        changed_by_id[task.id] = task.model_copy(update={"expected_material_attempts": attempts})

    with pytest.raises(ValueError, match="expected material attempt tools"):
        all_tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
        manifest.validate_tasks([changed_by_id.get(task.id, task) for task in all_tasks])
