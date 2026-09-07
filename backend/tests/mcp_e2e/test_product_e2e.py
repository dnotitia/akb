"""Authenticated MCP product behavior through the official Python SDK."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from uuid import uuid4

import pytest
from mcp import Client
from mcp import types as mcp_types

from .conftest import SecondaryMcpSession
from .runtime import RuntimeContext, redact_error


SCENARIO = "mcp_product_e2e"


def _new_vault_name(prefix: str) -> str:
    return f"mcp-sdk-{prefix}-{uuid4().hex[:10]}"


def _text_content(result: mcp_types.CallToolResult, operation: str) -> str:
    for item in result.content:
        if isinstance(item, mcp_types.TextContent):
            return item.text
    pytest.fail(f"scenario={SCENARIO} operation={operation}: tool returned no public JSON text")


async def _call_json(
    client: Client,
    runtime_session: RuntimeContext,
    name: str,
    arguments: dict[str, Any],
    *,
    expect_error: bool = False,
) -> dict[str, Any]:
    operation = f"tools/call {name}"
    try:
        result = await client.call_tool(name, arguments)
    except Exception as exc:
        pytest.fail(f"scenario={SCENARIO} operation={operation}: {redact_error(exc, runtime_session.secrets)}")

    if bool(result.is_error) != expect_error:
        state = "error" if result.is_error else "success"
        pytest.fail(
            f"scenario={SCENARIO} operation={operation}: expected "
            f"{('an error' if expect_error else 'success')}, got {state}"
        )

    try:
        public = json.loads(_text_content(result, operation))
    except (TypeError, ValueError) as exc:
        pytest.fail(f"scenario={SCENARIO} operation={operation}: tool returned invalid public JSON: {exc}")
    if not isinstance(public, dict):
        pytest.fail(f"scenario={SCENARIO} operation={operation}: public result is not an object")
    return public


async def _create_vault(
    client: Client,
    runtime_session: RuntimeContext,
    prefix: str,
    *,
    description: str = "MCP SDK behavior test",
) -> str:
    name = _new_vault_name(prefix)
    result = await _call_json(
        client,
        runtime_session,
        "akb_create_vault",
        {"name": name, "description": description},
    )
    assert result.get("name") == name
    assert isinstance(result.get("vault_id"), str)
    return name


async def test_documents_browse_search_and_versioned_reads(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "documents")

    created = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "specs",
            "title": "MCP Created Spec",
            "content": (
                "## API Spec\n\nCreated via MCP tool call.\n\n"
                "## Endpoints\n\nGET /api/v1/health"
            ),
            "type": "spec",
            "tags": ["mcp", "test"],
        },
    )
    doc_uri = created["uri"]
    assert isinstance(doc_uri, str)
    assert isinstance(created.get("chunks_indexed"), int)

    linked = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "plans",
            "title": "Migration Plan",
            "content": f"## Plan\n\nMigrate based on [API Spec]({created['path']}).",
            "type": "plan",
            "tags": ["mcp"],
            "depends_on": [doc_uri],
        },
    )
    linked_uri = linked["uri"]
    assert isinstance(linked_uri, str)

    fetched = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": doc_uri})
    assert fetched.get("title") == "MCP Created Spec"

    updated = await _call_json(
        mcp_client,
        runtime_session,
        "akb_update",
        {"uri": doc_uri, "status": "active", "message": "Promote via MCP"},
    )
    assert isinstance(updated.get("commit_hash"), str)

    root = await _call_json(mcp_client, runtime_session, "akb_browse", {"vault": vault})
    assert len(root.get("items", [])) >= 2
    specs = await _call_json(
        mcp_client,
        runtime_session,
        "akb_browse",
        {"vault": vault, "collection": "specs"},
    )
    assert len(specs.get("items", [])) >= 1

    search = await _call_json(mcp_client, runtime_session, "akb_search", {"query": "API spec endpoint"})
    assert type(search.get("total")) is int
    assert search["total"] >= 0

    drilled = await _call_json(mcp_client, runtime_session, "akb_drill_down", {"uri": doc_uri})
    sections = drilled.get("sections")
    assert isinstance(sections, list) and sections
    assert not any(
        re.match(r"^(TITLE|SUMMARY|TAGS|PATH|TYPE):", str(section.get("content") or ""))
        for section in sections
    )

    trailing = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": f"{doc_uri}/"})
    assert trailing.get("path") == "specs/mcp-created-spec.md"

    first_collision = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "collision probe", "content": "BODY_FIRST"},
    )
    second_collision = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "collision probe", "content": "BODY_SECOND"},
    )
    first_uri = first_collision["uri"]
    second_uri = second_collision["uri"]
    assert first_uri != second_uri
    first_body = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": first_uri})
    second_body = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": second_uri})
    assert "BODY_FIRST" in first_body["content"] and "BODY_SECOND" not in first_body["content"]
    assert "BODY_SECOND" in second_body["content"] and "BODY_FIRST" not in second_body["content"]

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "api", "content": "FIRST_ONLY_TAG"},
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "api v2", "content": "SECOND_ONLY_TAG"},
    )
    exact = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": f"akb://{vault}/doc/specs/api.md"},
    )
    assert "FIRST_ONLY_TAG" in exact["content"]
    assert "SECOND_ONLY_TAG" not in exact["content"]

    history = await _call_json(
        mcp_client,
        runtime_session,
        "akb_history",
        {"uri": doc_uri},
    )
    versions = history.get("history")
    assert isinstance(versions, list) and len(versions) >= 2
    version = versions[0].get("hash")
    assert isinstance(version, str)
    historical = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": doc_uri, "version": version},
    )
    assert not str(historical.get("content", "")).lstrip().startswith("---")


async def test_document_move_preserves_aliases_and_collision_rules(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "moves")

    original = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "movetest", "title": "Move Me", "content": "MOVE_BODY_TAG"},
    )
    old_uri = original["uri"]
    moved = await _call_json(
        mcp_client,
        runtime_session,
        "akb_move",
        {"uri": old_uri, "slug": "moved-final"},
    )
    new_uri = moved["uri"]
    assert new_uri != old_uri
    assert moved.get("action") == "moved"
    assert "MOVE_BODY_TAG" in (await _call_json(mcp_client, runtime_session, "akb_get", {"uri": new_uri}))["content"]
    assert "MOVE_BODY_TAG" in (await _call_json(mcp_client, runtime_session, "akb_get", {"uri": old_uri}))["content"]

    moved_again = await _call_json(
        mcp_client,
        runtime_session,
        "akb_move",
        {"uri": new_uri, "collection": "archive"},
    )
    archive_uri = moved_again["uri"]
    assert "/archive/" in archive_uri
    assert "MOVE_BODY_TAG" in (await _call_json(mcp_client, runtime_session, "akb_get", {"uri": old_uri}))["content"]
    assert "MOVE_BODY_TAG" in (await _call_json(mcp_client, runtime_session, "akb_get", {"uri": new_uri}))["content"]

    no_op = await _call_json(mcp_client, runtime_session, "akb_move", {"uri": archive_uri}, expect_error=True)
    assert no_op.get("code") == "invalid_argument"

    twin_a = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "dups", "title": "Twin", "content": "TWIN_A"},
    )
    twin_b = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "dupsrc", "title": "Twin", "content": "TWIN_B"},
    )
    twin_b_moved = await _call_json(
        mcp_client,
        runtime_session,
        "akb_move",
        {"uri": twin_b["uri"], "collection": "dups"},
    )
    assert twin_b_moved["uri"] != twin_a["uri"]
    twin_a_body = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": twin_a["uri"]})
    twin_b_body = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": twin_b_moved["uri"]})
    assert "TWIN_A" in twin_a_body["content"] and "TWIN_B" not in twin_a_body["content"]
    assert "TWIN_B" in twin_b_body["content"] and "TWIN_A" not in twin_b_body["content"]

    recycled = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "reuse", "title": "Recycle", "content": "ORIG_DOC"},
    )
    recycled_old = recycled["uri"]
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_move",
        {"uri": recycled_old, "slug": "recycled"},
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "reuse", "title": "Recycle", "content": "NEW_DOC"},
    )
    reused = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": recycled_old})
    assert "NEW_DOC" in reused["content"] and "ORIG_DOC" not in reused["content"]

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "sfxa", "title": "Sfx", "content": "S1"},
    )
    suffixed = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "sfxa", "title": "Sfx", "content": "S2"},
    )
    moved_suffix = await _call_json(
        mcp_client,
        runtime_session,
        "akb_move",
        {"uri": suffixed["uri"], "collection": "sfxb"},
    )
    assert suffixed["uri"] != moved_suffix["uri"]
    assert moved_suffix["uri"].endswith("/sfx.md")

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "rej", "title": "Taken", "content": "T1"},
    )
    mover = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "rej", "title": "Mover", "content": "T2"},
    )
    rejected = await _call_json(
        mcp_client,
        runtime_session,
        "akb_move",
        {"uri": mover["uri"], "slug": "taken"},
        expect_error=True,
    )
    assert rejected.get("code") == "conflict"
    malformed = await _call_json(
        mcp_client,
        runtime_session,
        "akb_move",
        {"uri": "not-a-valid-uri", "slug": "x"},
        expect_error=True,
    )
    assert malformed.get("code") == "invalid_uri"


async def test_relations_activity_provenance_and_document_deletion(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "relations")
    first = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "First", "content": "FIRST"},
    )
    second = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "plans",
            "title": "Second",
            "content": "SECOND",
            "depends_on": [first["uri"]],
        },
    )

    relations = await _call_json(
        mcp_client,
        runtime_session,
        "akb_relations",
        {"uri": second["uri"]},
    )
    assert len(relations.get("relations", [])) >= 1
    graph = await _call_json(mcp_client, runtime_session, "akb_graph", {"vault": vault})
    assert len(graph.get("nodes", [])) >= 2
    assert len(graph.get("edges", [])) >= 1
    provenance = await _call_json(
        mcp_client,
        runtime_session,
        "akb_provenance",
        {"uri": second["uri"]},
    )
    assert provenance.get("title")

    activity = await _call_json(mcp_client, runtime_session, "akb_activity", {"vault": vault})
    assert type(activity.get("returned")) is int and activity["returned"] >= 2
    assert activity["activity"] and activity["activity"][0].get("files")
    first_hash = activity["activity"][0].get("hash")
    assert isinstance(first_hash, str)
    diff = await _call_json(
        mcp_client,
        runtime_session,
        "akb_diff",
        {"uri": first["uri"], "commit": first_hash},
    )
    assert diff.get("type")

    username = os.environ[runtime_session.descriptor.username_env]
    users = await _call_json(
        mcp_client,
        runtime_session,
        "akb_search_users",
        {"query": username},
    )
    assert len(users.get("users", [])) >= 1
    info = await _call_json(mcp_client, runtime_session, "akb_vault_info", {"vault": vault})
    assert info.get("owner")
    members = await _call_json(mcp_client, runtime_session, "akb_vault_members", {"vault": vault})
    assert len(members.get("members", [])) >= 1

    deleted = await _call_json(mcp_client, runtime_session, "akb_delete", {"uri": first["uri"]})
    assert deleted.get("deleted") is True
    missing = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": first["uri"]}, expect_error=True)
    assert missing.get("code") == "not_found"

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_collection",
        {"vault": vault, "path": "keepempty"},
    )
    keep = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "keepempty", "title": "keep-t", "content": "## c"},
    )
    await _call_json(mcp_client, runtime_session, "akb_delete", {"uri": keep["uri"]})
    after_delete = await _call_json(mcp_client, runtime_session, "akb_browse", {"vault": vault})
    assert sum(
        item.get("name") == "keepempty" and item.get("type") == "collection"
        for item in after_delete.get("items", [])
    ) == 1


async def test_tables_sql_and_ddl(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "tables")
    table = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {
            "vault": vault,
            "name": "mcp_items",
            "columns": [{"name": "product", "type": "text"}, {"name": "qty", "type": "number"}],
        },
    )
    table_uri = table["uri"]
    assert isinstance(table_uri, str)

    overlong = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {
            "vault": vault,
            "name": "a" * 70,
            "columns": [{"name": "x", "type": "text"}],
        },
        expect_error=True,
    )
    assert overlong.get("code") == "invalid_argument"

    inserted = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO mcp_items (product, qty) VALUES ('Widget', 100), ('Gadget', 50)"},
    )
    assert inserted.get("result")
    rows = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT * FROM mcp_items"},
    )
    assert rows.get("total") == 2
    aggregate = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT SUM(qty) as total_qty, COUNT(*) as cnt FROM mcp_items"},
    )
    assert aggregate.get("items", [{}])[0].get("total_qty") == 150

    info = await _call_json(mcp_client, runtime_session, "akb_vault_info", {"vault": vault})
    tables = info.get("tables") or []
    table_info = next(item for item in tables if item.get("name") == "mcp_items")
    assert any(column.get("name") == "product" for column in table_info.get("columns", []))

    wrong_column = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT * FROM mcp_items WHERE producct = 'Widget'"},
        expect_error=True,
    )
    assert "Did you mean" in wrong_column.get("hint", "")
    assert "product" in (wrong_column.get("details") or {}).get("available_columns", [])
    wrong_table = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT * FROM mcp_itms"},
        expect_error=True,
    )
    assert "Did you mean" in wrong_table.get("hint", "")
    assert "mcp_items" in wrong_table.get("hint", "")

    browsed_tables = await _call_json(
        mcp_client,
        runtime_session,
        "akb_browse",
        {"vault": vault, "content_type": "tables"},
    )
    table_items = [item for item in browsed_tables.get("items", []) if item.get("type") == "table"]
    assert table_items
    sql_name = next(item.get("sql_name") for item in table_items if item.get("name") == "mcp_items")
    assert sql_name == "mcp_items"
    round_trip = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": f"SELECT COUNT(*) as cnt FROM {sql_name}"},
    )
    assert round_trip.get("items", [{}])[0].get("cnt") == 2

    altered = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {"uri": table_uri, "add_columns": [{"name": "category", "type": "text"}]},
    )
    assert any(column.get("name") == "category" for column in altered.get("columns", []))
    dropped = await _call_json(mcp_client, runtime_session, "akb_drop_table", {"uri": table_uri})
    assert dropped.get("deleted") is True
    remaining = await _call_json(
        mcp_client,
        runtime_session,
        "akb_browse",
        {"vault": vault, "content_type": "tables"},
    )
    assert not [item for item in remaining.get("items", []) if item.get("type") == "table"]


async def test_publication_help_and_vault_lifecycle(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "publication")
    document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "specs",
            "title": "Pub Test",
            "content": "## Public\n\nTest.",
        },
    )
    publication = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publish",
        {"uri": document["uri"]},
    )
    assert isinstance(publication.get("slug"), str) and publication["slug"]
    unpublished = await _call_json(
        mcp_client,
        runtime_session,
        "akb_unpublish",
        {"uri": document["uri"]},
    )
    assert unpublished.get("deleted", 0) >= 1

    help_root = await _call_json(mcp_client, runtime_session, "akb_help", {})
    assert "Quick Start" in help_root.get("help", "")
    help_sql = await _call_json(
        mcp_client,
        runtime_session,
        "akb_help",
        {"topic": "akb_sql"},
    )
    assert "SELECT" in help_sql.get("help", "")

    lifecycle = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_vault",
        {"name": _new_vault_name("lifecycle")},
    )
    lifecycle_vault = lifecycle["name"]
    archived = await _call_json(
        mcp_client,
        runtime_session,
        "akb_archive_vault",
        {"vault": lifecycle_vault},
    )
    assert archived.get("status") == "archived"
    blocked = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": lifecycle_vault,
            "collection": "test",
            "title": "Fail",
            "content": "#No",
        },
        expect_error=True,
    )
    assert blocked.get("code")
    deleted = await _call_json(
        mcp_client,
        runtime_session,
        "akb_delete_vault",
        {"vault": lifecycle_vault},
    )
    assert deleted.get("deleted") is True
    gone = await _call_json(
        mcp_client,
        runtime_session,
        "akb_browse",
        {"vault": lifecycle_vault},
        expect_error=True,
    )
    assert gone.get("code") == "not_found"


async def test_access_roles_and_public_levels(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "access")
    second = secondary_mcp_client.client

    no_access = await _call_json(
        second,
        runtime_session,
        "akb_browse",
        {"vault": vault},
        expect_error=True,
    )
    assert no_access.get("code")

    granted_reader = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "reader"},
    )
    assert granted_reader.get("granted") is True
    readable = await _call_json(second, runtime_session, "akb_browse", {"vault": vault})
    assert "items" in readable
    searchable = await _call_json(
        second,
        runtime_session,
        "akb_search",
        {"query": "test", "vault": vault},
    )
    assert "total" in searchable or "results" in searchable
    reader_write = await _call_json(
        second,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "hack", "title": "Unauthorized", "content": "# No"},
        expect_error=True,
    )
    assert reader_write.get("code")

    granted_writer = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "writer"},
    )
    assert granted_writer.get("granted") is True
    writer_doc = await _call_json(
        second,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "user2-docs", "title": "Writer Test", "content": "## Written"},
    )
    assert isinstance(writer_doc.get("uri"), str)

    revoked = await _call_json(
        mcp_client,
        runtime_session,
        "akb_revoke",
        {"vault": vault, "user": secondary_mcp_client.username},
    )
    assert revoked.get("revoked") is True
    await _call_json(second, runtime_session, "akb_browse", {"vault": vault}, expect_error=True)
    await _call_json(
        second,
        runtime_session,
        "akb_search",
        {"query": "test", "vault": vault},
        expect_error=True,
    )

    public_writer = await _call_json(
        mcp_client,
        runtime_session,
        "akb_set_public",
        {"vault": vault, "level": "writer"},
    )
    assert public_writer.get("public_access") == "writer"
    public_doc = await _call_json(
        second,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "public-test", "title": "Public Write", "content": "# Public"},
    )
    assert isinstance(public_doc.get("uri"), str)

    public_reader = await _call_json(
        mcp_client,
        runtime_session,
        "akb_set_public",
        {"vault": vault, "level": "reader"},
    )
    assert public_reader.get("public_access") == "reader"
    await _call_json(
        second,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "public-test", "title": "Should Fail", "content": "#No"},
        expect_error=True,
    )
    public_read = await _call_json(second, runtime_session, "akb_browse", {"vault": vault})
    assert "items" in public_read

    public_none = await _call_json(
        mcp_client,
        runtime_session,
        "akb_set_public",
        {"vault": vault, "level": "none"},
    )
    assert public_none.get("public_access") == "none"
    await _call_json(second, runtime_session, "akb_browse", {"vault": vault}, expect_error=True)
