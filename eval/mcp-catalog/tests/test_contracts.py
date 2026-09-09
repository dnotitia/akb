from __future__ import annotations

from pathlib import Path

import pytest

from mcp_catalog.contracts import (
    CatalogSnapshot,
    hash_json,
    load_run_manifest,
    load_task_corpus,
    source_blind_violations_for,
    token_estimate,
)

ROOT = Path(__file__).parents[1]


def test_registered_manifest_and_corpus_cover_every_category() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")

    manifest.validate_tasks(tasks)
    assert len(tasks) == 16
    assert {model.class_name for model in manifest.models} == {"primary", "lightweight"}
    assert {task.category for task in tasks} == set(manifest.category_minimums)
    assert source_blind_violations_for(tasks, manifest.operation_map) == []


def test_source_blind_check_rejects_tool_names_but_allows_logical_operations() -> None:
    task = load_task_corpus(ROOT / "corpus" / "tasks.json")[0]
    hinted = task.model_copy(update={"prompt": "사용 가능한 akb_get으로 읽어 줘."})

    violations = source_blind_violations_for([hinted], {"read": ["akb_get"]})

    assert violations == [(task.id, "akb_get")]


def test_catalog_hash_and_schema_preservation_are_deterministic() -> None:
    tools = [
        {
            "name": "records",
            "description": "A test catalog entry",
            "inputSchema": {
                "type": "object",
                "oneOf": [
                    {"type": "object", "properties": {"action": {"const": "read"}}},
                    {"type": "object", "properties": {"action": {"const": "write"}}},
                ],
            },
        }
    ]
    snapshot = CatalogSnapshot(
        transport="http",
        source_revision="0" * 40,
        artifact_version="0.0.0",
        tool_count=1,
        catalog_hash=hash_json(tools),
        catalog_token_estimate=token_estimate(tools),
        tools=tools,
    )

    assert snapshot.tools[0]["inputSchema"]["oneOf"][0]["properties"]["action"]["const"] == "read"
    assert snapshot.catalog_hash == hash_json(tools)
    assert snapshot.catalog_token_estimate == token_estimate(tools)


def test_catalog_snapshot_rejects_tampered_hash() -> None:
    tools = [{"name": "one", "inputSchema": {"type": "object"}}]

    with pytest.raises(ValueError, match="catalog_hash"):
        CatalogSnapshot(
            transport="http",
            source_revision="0" * 40,
            artifact_version="0.0.0",
            tool_count=1,
            catalog_hash="0" * 64,
            catalog_token_estimate=token_estimate(tools),
            tools=tools,
        )
