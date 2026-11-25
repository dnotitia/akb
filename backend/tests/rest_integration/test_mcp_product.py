"""Authenticated MCP product behavior through the official Python SDK."""

import re
from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from mcp import Client

from .helpers import Actor
from .mcp_client import (
    SecondaryMcpSession,
    call_json,
    create_vault,
    mcp_actor as mcp_actor,
    mcp_client as mcp_client,
    secondary_mcp_client as secondary_mcp_client,
)
from .search_fixtures import drain_search_index as drain_search_index

pytestmark = pytest.mark.live_services


async def test_drill_down_outline_filters_before_the_heading_cap(
    mcp_client: Client,
) -> None:
    vault = await create_vault(mcp_client, "outline")
    content = "".join(f"# Early {i}\nbody\n" for i in range(51))
    content += "# 설치\nintro\n## 설정\nchild\n# 설치법\nsibling\n"
    doc = await call_json(mcp_client, "akb_put", {
        "vault": vault, "title": "Guide", "content": content,
    })
    args = {"uri": doc["uri"], "mode": "outline"}
    full = await call_json(mcp_client, "akb_drill_down", args)
    assert full["returned"] == 50 and full["truncated"] is True
    assert "# 설치" not in full["outline"]

    narrowed = await call_json(mcp_client, "akb_drill_down", {**args, "section": "# 설치"})
    assert narrowed["outline"] == ["# 설치", "# 설치 > ## 설정"]
    assert narrowed["returned"] == narrowed["total"] == 2
    sections = await call_json(mcp_client, "akb_drill_down", {
        "uri": doc["uri"], "section": "설치", "pattern": "sibling",
    })
    assert [row["section_path"] for row in sections["sections"]] == ["# 설치법"]


async def test_documents_browse_search_and_versioned_reads(
    mcp_client: Client,
    drain_search_index: Callable[[], Awaitable[None]],
) -> None:
    vault = await create_vault(mcp_client, "documents")

    created = await call_json(
        mcp_client,
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

    linked = await call_json(
        mcp_client,
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

    fetched = await call_json(mcp_client, "akb_get", {"uri": doc_uri})
    assert fetched.get("title") == "MCP Created Spec"

    updated = await call_json(
        mcp_client,
        "akb_update",
        {"uri": doc_uri, "status": "active", "message": "Promote via MCP"},
    )
    assert isinstance(updated.get("commit_hash"), str)

    root = await call_json(mcp_client, "akb_browse", {"vault": vault})
    assert len(root.get("items", [])) >= 2
    specs = await call_json(
        mcp_client,
        "akb_browse",
        {"vault": vault, "collection": "specs"},
    )
    assert len(specs.get("items", [])) >= 1

    await drain_search_index()
    search = await call_json(mcp_client, "akb_search", {"query": "API spec endpoint"})
    assert type(search.get("total")) is int
    assert search["total"] >= 1
    assert any(result["uri"] == doc_uri for result in search["results"])

    drilled = await call_json(mcp_client, "akb_drill_down", {"uri": doc_uri})
    sections = drilled.get("sections")
    assert isinstance(sections, list) and sections
    assert not any(
        re.match(r"^(TITLE|SUMMARY|TAGS|PATH|TYPE):", str(section.get("content") or ""))
        for section in sections
    )

    trailing = await call_json(mcp_client, "akb_get", {"uri": f"{doc_uri}/"})
    assert trailing.get("path") == "specs/mcp-created-spec.md"

    first_collision = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "collision probe", "content": "BODY_FIRST"},
    )
    second_collision = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "collision probe", "content": "BODY_SECOND"},
    )
    first_uri = first_collision["uri"]
    second_uri = second_collision["uri"]
    assert first_uri != second_uri
    first_body = await call_json(mcp_client, "akb_get", {"uri": first_uri})
    second_body = await call_json(mcp_client, "akb_get", {"uri": second_uri})
    assert "BODY_FIRST" in first_body["content"] and "BODY_SECOND" not in first_body["content"]
    assert "BODY_SECOND" in second_body["content"] and "BODY_FIRST" not in second_body["content"]

    await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "api", "content": "FIRST_ONLY_TAG"},
    )
    await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "api v2", "content": "SECOND_ONLY_TAG"},
    )
    exact = await call_json(
        mcp_client,
        "akb_get",
        {"uri": f"akb://{vault}/doc/specs/api.md"},
    )
    assert "FIRST_ONLY_TAG" in exact["content"]
    assert "SECOND_ONLY_TAG" not in exact["content"]

    history = await call_json(
        mcp_client,
        "akb_history",
        {"uri": doc_uri},
    )
    versions = history.get("history")
    assert isinstance(versions, list) and len(versions) >= 2
    version = versions[0].get("hash")
    assert isinstance(version, str)
    historical = await call_json(
        mcp_client,
        "akb_get",
        {"uri": doc_uri, "version": version},
    )
    assert not str(historical.get("content", "")).lstrip().startswith("---")


async def test_document_move_preserves_aliases_and_collision_rules(
    mcp_client: Client,
) -> None:
    vault = await create_vault(mcp_client, "moves")

    original = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "movetest", "title": "Move Me", "content": "MOVE_BODY_TAG"},
    )
    old_uri = original["uri"]
    moved = await call_json(
        mcp_client,
        "akb_move",
        {"uri": old_uri, "slug": "moved-final"},
    )
    new_uri = moved["uri"]
    assert new_uri != old_uri
    assert moved.get("action") == "moved"
    assert "MOVE_BODY_TAG" in (await call_json(mcp_client, "akb_get", {"uri": new_uri}))["content"]
    assert "MOVE_BODY_TAG" in (await call_json(mcp_client, "akb_get", {"uri": old_uri}))["content"]

    moved_again = await call_json(
        mcp_client,
        "akb_move",
        {"uri": new_uri, "collection": "archive"},
    )
    archive_uri = moved_again["uri"]
    assert "/archive/" in archive_uri
    assert "MOVE_BODY_TAG" in (await call_json(mcp_client, "akb_get", {"uri": old_uri}))["content"]
    assert "MOVE_BODY_TAG" in (await call_json(mcp_client, "akb_get", {"uri": new_uri}))["content"]

    no_op = await call_json(mcp_client, "akb_move", {"uri": archive_uri}, expect_error=True)
    assert no_op.get("code") == "invalid_argument"

    twin_a = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "dups", "title": "Twin", "content": "TWIN_A"},
    )
    twin_b = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "dupsrc", "title": "Twin", "content": "TWIN_B"},
    )
    twin_b_moved = await call_json(
        mcp_client,
        "akb_move",
        {"uri": twin_b["uri"], "collection": "dups"},
    )
    assert twin_b_moved["uri"] != twin_a["uri"]
    twin_a_body = await call_json(mcp_client, "akb_get", {"uri": twin_a["uri"]})
    twin_b_body = await call_json(mcp_client, "akb_get", {"uri": twin_b_moved["uri"]})
    assert "TWIN_A" in twin_a_body["content"] and "TWIN_B" not in twin_a_body["content"]
    assert "TWIN_B" in twin_b_body["content"] and "TWIN_A" not in twin_b_body["content"]

    recycled = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "reuse", "title": "Recycle", "content": "ORIG_DOC"},
    )
    recycled_old = recycled["uri"]
    await call_json(
        mcp_client,
        "akb_move",
        {"uri": recycled_old, "slug": "recycled"},
    )
    await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "reuse", "title": "Recycle", "content": "NEW_DOC"},
    )
    reused = await call_json(mcp_client, "akb_get", {"uri": recycled_old})
    assert "NEW_DOC" in reused["content"] and "ORIG_DOC" not in reused["content"]

    await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "sfxa", "title": "Sfx", "content": "S1"},
    )
    suffixed = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "sfxa", "title": "Sfx", "content": "S2"},
    )
    moved_suffix = await call_json(
        mcp_client,
        "akb_move",
        {"uri": suffixed["uri"], "collection": "sfxb"},
    )
    assert suffixed["uri"] != moved_suffix["uri"]
    assert moved_suffix["uri"].endswith("/sfx.md")

    await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "rej", "title": "Taken", "content": "T1"},
    )
    mover = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "rej", "title": "Mover", "content": "T2"},
    )
    rejected = await call_json(
        mcp_client,
        "akb_move",
        {"uri": mover["uri"], "slug": "taken"},
        expect_error=True,
    )
    assert rejected.get("code") == "conflict"
    malformed = await call_json(
        mcp_client,
        "akb_move",
        {"uri": "not-a-valid-uri", "slug": "x"},
        expect_error=True,
    )
    assert malformed.get("code") == "invalid_uri"


async def test_relations_activity_provenance_and_document_deletion(
    mcp_client: Client,
    mcp_actor: Actor,
) -> None:
    vault = await create_vault(mcp_client, "relations")
    first = await call_json(
        mcp_client,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "First", "content": "FIRST"},
    )
    second = await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": vault,
            "collection": "plans",
            "title": "Second",
            "content": "SECOND",
            "depends_on": [first["uri"]],
        },
    )

    relations = await call_json(
        mcp_client,
        "akb_relations",
        {"uri": second["uri"]},
    )
    assert len(relations.get("relations", [])) >= 1
    graph = await call_json(mcp_client, "akb_graph", {"vault": vault})
    assert len(graph.get("nodes", [])) >= 2
    assert len(graph.get("edges", [])) >= 1
    provenance = await call_json(
        mcp_client,
        "akb_provenance",
        {"uri": second["uri"]},
    )
    assert provenance.get("title")
    assert {"doc_id", "created_by", "created_at", "updated_at", "current_commit", "relations"} <= provenance.keys()

    activity = await call_json(mcp_client, "akb_activity", {"vault": vault})
    assert type(activity.get("returned")) is int and activity["returned"] >= 2
    assert activity["activity"] and activity["activity"][0].get("files")
    first_hash = activity["activity"][0].get("hash")
    assert isinstance(first_hash, str)
    diff = await call_json(
        mcp_client,
        "akb_diff",
        {"uri": first["uri"], "commit": first_hash},
    )
    assert diff.get("type")

    users = await call_json(
        mcp_client,
        "akb_search_users",
        {"query": mcp_actor.username},
    )
    assert len(users.get("users", [])) >= 1
    info = await call_json(mcp_client, "akb_vault_info", {"vault": vault})
    assert info.get("owner")
    members = await call_json(mcp_client, "akb_vault_members", {"vault": vault})
    assert len(members.get("members", [])) >= 1

    deleted = await call_json(mcp_client, "akb_delete", {"uri": first["uri"]})
    assert deleted.get("deleted") is True
    missing = await call_json(mcp_client, "akb_get", {"uri": first["uri"]}, expect_error=True)
    assert missing.get("code") == "not_found"


async def test_tables_sql_and_ddl(
    mcp_client: Client,
) -> None:
    vault = await create_vault(mcp_client, "tables")
    table = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "mcp_items",
            "columns": [{"name": "product", "type": "text"}, {"name": "qty", "type": "number"}],
        },
    )
    table_uri = table["uri"]
    assert isinstance(table_uri, str)

    overlong = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "a" * 70,
            "columns": [{"name": "x", "type": "text"}],
        },
        expect_error=True,
    )
    assert overlong.get("code") == "invalid_argument"

    inserted = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO mcp_items (product, qty) VALUES ('Widget', 100), ('Gadget', 50)"},
    )
    assert inserted.get("result")
    rows = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT * FROM mcp_items"},
    )
    assert str(rows.get("total")) == "2"
    aggregate = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT SUM(qty) as total_qty, COUNT(*) as cnt FROM mcp_items"},
    )
    assert str(aggregate.get("items", [{}])[0].get("total_qty")) == "150"

    info = await call_json(mcp_client, "akb_vault_info", {"vault": vault})
    tables = info.get("tables") or []
    table_info = next(item for item in tables if item.get("name") == "mcp_items")
    assert any(column.get("name") == "product" for column in table_info.get("columns", []))

    wrong_column = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT * FROM mcp_items WHERE producct = 'Widget'"},
        expect_error=True,
    )
    assert "Did you mean" in wrong_column.get("hint", "")
    assert "product" in (wrong_column.get("details") or {}).get("available_columns", [])
    wrong_table = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT * FROM mcp_itms"},
        expect_error=True,
    )
    assert "Did you mean" in wrong_table.get("hint", "")
    assert "mcp_items" in wrong_table.get("hint", "")

    browsed_tables = await call_json(
        mcp_client,
        "akb_browse",
        {"vault": vault, "content_type": "tables"},
    )
    table_items = [item for item in browsed_tables.get("items", []) if item.get("type") == "table"]
    assert table_items
    sql_name = next(item.get("sql_name") for item in table_items if item.get("name") == "mcp_items")
    assert sql_name == "mcp_items"
    round_trip = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": f"SELECT COUNT(*) as cnt FROM {sql_name}"},
    )
    assert str(round_trip.get("items", [{}])[0].get("cnt")) == "2"

    altered = await call_json(
        mcp_client,
        "akb_alter_table",
        {"uri": table_uri, "add_columns": [{"name": "category", "type": "text"}]},
    )
    assert any(column.get("name") == "category" for column in altered.get("columns", []))
    dropped = await call_json(mcp_client, "akb_drop_table", {"uri": table_uri})
    assert dropped.get("deleted") is True
    remaining = await call_json(
        mcp_client,
        "akb_browse",
        {"vault": vault, "content_type": "tables"},
    )
    assert not [item for item in remaining.get("items", []) if item.get("type") == "table"]


async def test_publication_help_and_vault_lifecycle(
    mcp_client: Client,
) -> None:
    vault = await create_vault(mcp_client, "publication")
    document = await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": vault,
            "collection": "specs",
            "title": "Pub Test",
            "content": "## Public\n\nTest.",
        },
    )
    publication = await call_json(
        mcp_client,
        "akb_publish",
        {"uri": document["uri"]},
    )
    assert isinstance(publication.get("slug"), str) and publication["slug"]
    unpublished = await call_json(
        mcp_client,
        "akb_unpublish",
        {"uri": document["uri"]},
    )
    assert unpublished.get("deleted", 0) >= 1

    help_root = await call_json(mcp_client, "akb_help", {})
    assert "Quick Start" in help_root.get("help", "")
    help_sql = await call_json(
        mcp_client,
        "akb_help",
        {"topic": "akb_sql"},
    )
    assert "SELECT" in help_sql.get("help", "")

    lifecycle = await call_json(
        mcp_client,
        "akb_create_vault",
        {"name": f"mcp-sdk-lifecycle-{uuid4().hex[:10]}"},
    )
    lifecycle_vault = lifecycle["name"]
    archived = await call_json(
        mcp_client,
        "akb_archive_vault",
        {"vault": lifecycle_vault},
    )
    assert archived.get("status") == "archived"
    blocked = await call_json(
        mcp_client,
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
    deleted = await call_json(
        mcp_client,
        "akb_delete_vault",
        {"vault": lifecycle_vault},
    )
    assert deleted.get("deleted") is True
    gone = await call_json(
        mcp_client,
        "akb_browse",
        {"vault": lifecycle_vault},
        expect_error=True,
    )
    assert gone.get("code") == "not_found"


async def test_access_roles_and_public_levels(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    drain_search_index: Callable[[], Awaitable[None]],
) -> None:
    vault = await create_vault(mcp_client, "access")
    second = secondary_mcp_client.client
    searchable_doc = await call_json(mcp_client, "akb_put", {
        "vault": vault, "title": "Reader search test", "content": "test",
    })
    await drain_search_index()

    no_access = await call_json(
        second,
        "akb_browse",
        {"vault": vault},
        expect_error=True,
    )
    assert no_access.get("code")

    granted_reader = await call_json(
        mcp_client,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "reader"},
    )
    assert granted_reader.get("granted") is True
    explanation = await call_json(second, "akb_explain_access", {
        "vault": vault, "user": secondary_mcp_client.username,
    })
    assert explanation["effective_role"] == explanation["derived_role"] == "reader"
    assert explanation["contributions"] and explanation["non_member_paths"]["public_access"] == "none"

    denied_delete = await call_json(
        second, "akb_delete_vault", {"vault": vault}, expect_error=True,
    )
    assert denied_delete["code"] == "permission_denied"
    readable = await call_json(second, "akb_browse", {"vault": vault})
    assert "items" in readable
    searchable = await call_json(
        second,
        "akb_search",
        {"query": "test", "vault": vault},
    )
    assert "total" in searchable or "results" in searchable
    assert any(result["uri"] == searchable_doc["uri"] for result in searchable["results"])
    reader_write = await call_json(
        second,
        "akb_put",
        {"vault": vault, "collection": "hack", "title": "Unauthorized", "content": "# No"},
        expect_error=True,
    )
    assert reader_write.get("code")

    granted_writer = await call_json(
        mcp_client,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "writer"},
    )
    assert granted_writer.get("granted") is True
    writer_doc = await call_json(
        second,
        "akb_put",
        {"vault": vault, "collection": "user2-docs", "title": "Writer Test", "content": "## Written"},
    )
    assert isinstance(writer_doc.get("uri"), str)

    revoked = await call_json(
        mcp_client,
        "akb_revoke",
        {"vault": vault, "user": secondary_mcp_client.username},
    )
    assert revoked.get("revoked") is True
    await call_json(second, "akb_browse", {"vault": vault}, expect_error=True)
    await call_json(
        second,
        "akb_search",
        {"query": "test", "vault": vault},
        expect_error=True,
    )

    public_writer = await call_json(
        mcp_client,
        "akb_set_public",
        {"vault": vault, "level": "writer"},
    )
    assert public_writer.get("public_access") == "writer"
    public_doc = await call_json(
        second,
        "akb_put",
        {"vault": vault, "collection": "public-test", "title": "Public Write", "content": "# Public"},
    )
    assert isinstance(public_doc.get("uri"), str)

    public_reader = await call_json(
        mcp_client,
        "akb_set_public",
        {"vault": vault, "level": "reader"},
    )
    assert public_reader.get("public_access") == "reader"
    await call_json(
        second,
        "akb_put",
        {"vault": vault, "collection": "public-test", "title": "Should Fail", "content": "#No"},
        expect_error=True,
    )
    public_read = await call_json(second, "akb_browse", {"vault": vault})
    assert "items" in public_read

    public_none = await call_json(
        mcp_client,
        "akb_set_public",
        {"vault": vault, "level": "none"},
    )
    assert public_none.get("public_access") == "none"
    await call_json(second, "akb_browse", {"vault": vault}, expect_error=True)

    await call_json(mcp_client, "akb_grant", {
        "vault": vault, "user": secondary_mcp_client.username, "role": "admin",
    })
    deleted = await call_json(second, "akb_delete_vault", {"vault": vault})
    assert deleted["deleted"] is True
