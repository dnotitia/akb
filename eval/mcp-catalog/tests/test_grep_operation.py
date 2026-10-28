from __future__ import annotations

from pathlib import Path

from mcp_catalog.contracts import load_run_manifest, load_task_corpus, public_arguments_match_schema
from mcp_catalog.execution import bind_tool_calls, logical_operation_for

ROOT = Path(__file__).parents[1]


def test_akb_grep_uses_replace_presence_and_json_schema_type() -> None:
    operation_map = load_run_manifest(ROOT / "config" / "run.json").operation_map
    assert logical_operation_for("akb_grep", operation_map, {"pattern": "needle"}) == "search"
    assert logical_operation_for(
        "akb_grep", operation_map, {"pattern": "needle", "replace": ""}
    ) == "update"
    assert logical_operation_for(
        "akb_grep", operation_map, {"pattern": "needle", "replace": "replacement"}
    ) == "update"
    assert logical_operation_for(
        "akb_grep", operation_map, {"pattern": "needle", "replace": None}
    ) == "unknown"
    assert logical_operation_for(
        "akb_grep", operation_map, {"pattern": "needle", "replace": 1}
    ) == "unknown"

    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1},
            "replace": {"type": "string"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    assert public_arguments_match_schema({"pattern": "needle"}, schema)
    assert public_arguments_match_schema({"pattern": "needle", "replace": ""}, schema)
    assert not public_arguments_match_schema({"pattern": "needle", "replace": None}, schema)
    assert not public_arguments_match_schema({"pattern": "needle", "replace": 1}, schema)

    manifest = load_run_manifest(ROOT / "config" / "run.json")
    records = bind_tool_calls(
        [
            ("akb_grep", {"pattern": "needle", "replace": ""}),
            ("akb_grep", {"pattern": "needle", "replace": None}),
        ],
        [],
        manifest.operation_map,
        (),
        input_schemas={"akb_grep": schema},
        tool_resources=manifest.tool_resources,
        public_operations=manifest.public_operations,
    )
    assert records[0].logical_operation == "update"
    assert records[0].public_schema_valid is True
    assert records[1].logical_operation == "unknown"
    assert records[1].public_schema_valid is False


def test_grep_write_corpus_uses_the_current_public_tool() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    grep_write_tasks = [task for task in tasks if task.pair_id == "grep-write"]

    assert len(grep_write_tasks) == 2
    assert all("akb_grep" in task.coverage_tools for task in grep_write_tasks)
    assert all(
        any(
            attempt.logical_operation == "update"
            and attempt.tool_name == "akb_grep"
            and attempt.arguments.get("replace") == "status: active"
            for attempt in task.expected_material_attempts
        )
        for task in grep_write_tasks
    )
    assert "akb_grep" in manifest.operation_map["search"]
    assert "akb_grep" not in manifest.operation_map["update"]
