"""Detailed MCP product regressions through the official Python SDK."""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any

import httpx
from mcp import Client

from .conftest import SecondaryMcpSession
from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault


async def _search_until_found(
    client: Client,
    runtime_session: RuntimeContext,
    query: str,
    *,
    vault: str,
) -> dict[str, Any]:
    """Allow the derived search index to catch up without weakening the result."""

    result: dict[str, Any] = {}
    # akb_put commits PG before the repository-owned embed/index worker catches
    # up. The shell E2E contract allows a 180-second indexing window; keep the
    # same bound here so the fixed embedding stub's dense leg does not turn a
    # valid BM25 match into a timing race.
    for _ in range(90):
        result = await _call_json(
            client,
            runtime_session,
            "akb_search",
            {"query": query, "vault": vault},
        )
        if int(result.get("total", 0)) >= 1 or result.get("results"):
            return result
        await asyncio.sleep(2)
    return result


def _relation_count(result: dict[str, Any]) -> int:
    return len(result.get("outgoing", [])) + len(result.get("relations", []))


async def test_whole_body_update_requires_original_read_predicates(
    mcp_client: Client, runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "write-predicates")
    original = await _call_json(mcp_client, runtime_session, "akb_put", {
        "vault": vault, "collection": "notes", "title": "Predicate contract", "content": "body A",
    })
    uri = original["uri"]
    for content in ("stale body", ""):
        rejected = await _call_json(mcp_client, runtime_session, "akb_update", {
            "uri": uri, "content": content,
        }, expect_error=True)
        assert rejected["code"] == "invalid_argument"
    for token in ("", " ", "a" * 40 + "\x00"):
        rejected = await _call_json(mcp_client, runtime_session, "akb_update", {
            "uri": uri, "content": "private rejected body", "expected_commit": token,
        }, expect_error=True)
        assert rejected["code"] == "invalid_argument"
        assert "private rejected body" not in str(rejected)

    await _call_json(mcp_client, runtime_session, "akb_update", {
        "uri": uri, "content": None, "summary": "metadata only",
    })
    changed = await _call_json(mcp_client, runtime_session, "akb_update", {
        "uri": uri, "content": "body B", "expected_content_hash": original["content_hash"],
    })
    for conditions in (
        {"expected_commit": original["current_commit"], "expected_content_hash": changed["content_hash"]},
        {"expected_commit": changed["current_commit"], "expected_content_hash": original["content_hash"]},
    ):
        conflict = await _call_json(mcp_client, runtime_session, "akb_update", {
            "uri": uri, "content": "must not publish", **conditions,
        }, expect_error=True)
        assert conflict["code"] == "conflict"
    await _call_json(mcp_client, runtime_session, "akb_update", {
        "uri": uri, "content": "", "expected_commit": changed["current_commit"],
        "expected_content_hash": changed["content_hash"],
    })
    current = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": uri})
    assert current["content"] == "" and current["summary"] == "metadata only"


async def test_conflict_recovery_reapplies_intent_to_latest_without_token_replacement(
    mcp_client: Client, runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "recover-intent")
    created = await _call_json(mcp_client, runtime_session, "akb_put", {
        "vault": vault, "collection": "notes", "title": "Shared note",
        "content": "Original spelling: teh\n",
    })
    uri = created["uri"]
    original = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": uri})
    local_draft = original["content"].replace("teh", "the")
    headers = {"Authorization": f"Bearer {runtime_session.pat}"}
    async with httpx.AsyncClient(base_url=runtime_session.descriptor.app_origin, headers=headers) as web:
        path = f"/api/v1/documents/{vault}/{created['path']}"
        remote = await web.patch(path, json={
            "content": original["content"] + "Added by the web editor\n",
            "title": "Web title", "expected_commit": original["current_commit"],
        })
        assert remote.status_code == 200
        head = await web.get(f"/api/v1/document-head/{vault}/{created['path']}")
        assert head.status_code == 200
        assert head.json()["current_commit"] == remote.json()["current_commit"]
        assert "content" not in head.json() and head.headers["cache-control"] == "no-store"
        rejected = await _call_json(mcp_client, runtime_session, "akb_update", {
            "uri": uri, "content": local_draft, "expected_commit": original["current_commit"],
        }, expect_error=True)
        assert rejected["code"] == "conflict"
        latest = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": uri})
        assert "Added by the web editor" not in local_draft
        assert original["current_commit"] != latest["current_commit"]
        combined = latest["content"].replace("teh", "the")
        await _call_json(mcp_client, runtime_session, "akb_update", {
            "uri": uri, "content": combined, "expected_commit": latest["current_commit"],
        })
        final = await web.get(path)
        assert final.status_code == 200
        assert final.json()["content"] == combined
        assert "Added by the web editor" in final.json()["content"]
        assert "spelling: the" in final.json()["content"]
        assert final.json()["title"] == "Web title"


async def test_exact_text_edit_contract_and_permissions(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    runtime_session: RuntimeContext,
) -> None:
    """Preserve exact-text edit semantics, errors, history, and writer ACL."""

    catalog = await mcp_client.list_tools(cache_mode="bypass")
    tool_names = {tool.name for tool in catalog.tools}
    assert "akb_edit" in tool_names
    assert "akb_patch" not in tool_names
    edit_tool = next(tool for tool in catalog.tools if tool.name == "akb_edit")
    assert "old_string" in edit_tool.input_schema.get("required", [])

    vault = await _create_vault(mcp_client, runtime_session, "edit-detail")
    document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "edit-tests",
            "title": "Edit Target",
            "content": (
                "# Introduction\n\nThis is the original introduction paragraph.\n\n"
                "## Section A\n\nContent of section A is here.\n\n"
                "## Section B\n\nContent of section B is here.\n\n"
                "## Conclusion\n\nOriginal conclusion."
            ),
        },
    )
    document_uri = document["uri"]

    edited = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": document_uri,
            "old_string": "Content of section A is here.",
            "new_string": "Content of section A has been updated.",
            "message": "Update section A",
        },
    )
    assert edited.get("commit_hash")
    assert edited.get("chunks_indexed", 0) > 0
    current = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": document_uri})
    assert "Content of section A has been updated." in current["content"]
    assert "Content of section A is here." not in current["content"]

    not_found = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": document_uri,
            "old_string": "this string definitely does not exist in document xyz",
            "new_string": "replacement",
        },
        expect_error=True,
    )
    assert not_found.get("code") == "edit_failed"
    assert "not found" in str(not_found.get("error", "")).lower()
    assert "hint" in not_found

    duplicate = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "edit-tests",
            "title": "Duplicate Content",
            "content": "# Doc\n\nDUPLICATE LINE\nother stuff\nDUPLICATE LINE\nmore stuff\nDUPLICATE LINE",
        },
    )
    not_unique = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": duplicate["uri"],
            "old_string": "DUPLICATE LINE",
            "new_string": "X",
        },
        expect_error=True,
    )
    assert not_unique.get("code") == "edit_failed"
    assert "appears 3 times" in str(not_unique.get("error", ""))

    replaced = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": duplicate["uri"],
            "old_string": "DUPLICATE LINE",
            "new_string": "REPLACED",
            "replace_all": True,
        },
    )
    assert replaced.get("commit_hash")
    duplicate_body = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": duplicate["uri"]},
    )
    assert duplicate_body["content"].count("REPLACED") == 3
    assert "DUPLICATE LINE" not in duplicate_body["content"]

    empty_old = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {"uri": document_uri, "old_string": "", "new_string": "anything"},
        expect_error=True,
    )
    assert empty_old.get("code") == "edit_failed"
    assert "empty" in str(empty_old.get("error", "")).lower()

    no_op = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": document_uri,
            "old_string": "Original conclusion.",
            "new_string": "Original conclusion.",
        },
    )
    assert no_op.get("chunks_indexed") == 0

    deletion = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "edit-tests",
            "title": "Deletion Test",
            "content": "# Start\n\nkeep this\nDELETE_ME_LINE\nkeep that",
        },
    )
    deletion_edit = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": deletion["uri"],
            "old_string": "DELETE_ME_LINE\n",
            "new_string": "",
        },
    )
    assert deletion_edit.get("commit_hash")
    deletion_body = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": deletion["uri"]},
    )
    assert "DELETE_ME_LINE" not in deletion_body["content"]
    assert "keep this" in deletion_body["content"] and "keep that" in deletion_body["content"]

    multiline = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "edit-tests",
            "title": "Multiline Test",
            "content": "# Doc\n\n## Old Section\n\nLine one\nLine two\nLine three\n\n## Keep\n\nkeep me",
        },
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": multiline["uri"],
            "old_string": "## Old Section\n\nLine one\nLine two\nLine three",
            "new_string": "## New Section\n\nRewritten entirely.",
        },
    )
    multiline_body = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": multiline["uri"]},
    )
    assert "New Section" in multiline_body["content"]
    assert "Line one" not in multiline_body["content"]
    assert "keep me" in multiline_body["content"]

    literal = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "edit-tests",
            "title": "Regex Literal Test",
            "content": "# Regex Test\n\nVersion check: foo.*bar\nPattern: a[bc]+d",
        },
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": literal["uri"],
            "old_string": "foo.*bar",
            "new_string": "REPLACED_FOO_BAR",
        },
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": literal["uri"],
            "old_string": "a[bc]+d",
            "new_string": "CHARCLASS_GONE",
        },
    )
    literal_body = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": literal["uri"]},
    )
    assert "REPLACED_FOO_BAR" in literal_body["content"]
    assert "CHARCLASS_GONE" in literal_body["content"]
    assert "foo.*bar" not in literal_body["content"]

    frontmatter = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {"uri": document_uri, "old_string": "Edit Target", "new_string": "Hacked Title"},
        expect_error=True,
    )
    assert frontmatter.get("code") == "edit_failed"
    unchanged = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": document_uri})
    assert unchanged.get("title") == "Edit Target"

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "reader"},
    )
    reader_edit = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_edit",
        {
            "uri": document_uri,
            "old_string": "Content of section B is here.",
            "new_string": "reader attempted edit",
        },
        expect_error=True,
    )
    assert any(marker in str(reader_edit).lower() for marker in ("403", "forbidden", "permission", "writer", "role"))

    history = await _call_json(mcp_client, runtime_session, "akb_history", {"uri": document_uri})
    assert any("edit" in str(entry).lower() for entry in history.get("history", []))

    missing_document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_edit",
        {
            "uri": f"akb://{vault}/doc/nonexistent/missing.md",
            "old_string": "a",
            "new_string": "b",
        },
        expect_error=True,
    )
    assert "error" in missing_document or "not found" in str(missing_document).lower()

    edit_help = await _call_json(
        mcp_client,
        runtime_session,
        "akb_help",
        {"topic": "akb_edit"},
    )
    assert "akb_edit" in edit_help.get("help", "")
    assert "old_string" in edit_help.get("help", "")
    documents_help = await _call_json(
        mcp_client,
        runtime_session,
        "akb_help",
        {"topic": "documents"},
    )
    assert "akb_edit" in documents_help.get("help", "")
    assert "akb_patch" not in documents_help.get("help", "")


async def test_document_body_hash_and_expected_content_occ(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    """Keep body hashes stable across metadata updates and reject stale OCC."""

    vault = await _create_vault(mcp_client, runtime_session, "hash-detail")
    body = "# Hash Contract\n\nStable body for hashing."
    expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    created = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "specs",
            "title": "Hash Contract",
            "content": body,
            "type": "spec",
        },
    )
    document_uri = created["uri"]
    assert created.get("content_hash") == expected_hash
    assert created.get("current_commit")

    fetched = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": document_uri})
    assert fetched.get("content_hash") == expected_hash
    assert fetched.get("current_commit") == created.get("current_commit")

    metadata_update = await _call_json(
        mcp_client,
        runtime_session,
        "akb_update",
        {
            "uri": document_uri,
            "summary": "metadata only",
            "expected_content_hash": expected_hash,
        },
    )
    assert metadata_update.get("content_hash") == expected_hash
    assert metadata_update.get("current_commit") != created.get("current_commit")

    stale = await _call_json(
        mcp_client,
        runtime_session,
        "akb_update",
        {
            "uri": document_uri,
            "content": "changed",
            "expected_content_hash": hashlib.sha256(b"stale").hexdigest(),
        },
        expect_error=True,
    )
    assert "content_hash" in str(stale).lower()

    browsed = await _call_json(
        mcp_client,
        runtime_session,
        "akb_browse",
        {"vault": vault, "content_type": "documents", "include_hashes": True},
    )
    item = next(item for item in browsed.get("items", []) if item.get("uri") == document_uri)
    assert item.get("content_hash") == expected_hash


async def test_collection_lifecycle_boundaries(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    """Preserve collection normalization, idempotency, and deletion boundaries."""

    vault = await _create_vault(mcp_client, runtime_session, "collection-detail")

    created = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_collection",
        {"vault": vault, "path": "specs"},
    )
    assert created.get("ok") is True
    assert created.get("created") is True
    assert created.get("collection", {}).get("path") == "specs"
    assert created.get("collection", {}).get("doc_count") == 0
    browsed = await _call_json(mcp_client, runtime_session, "akb_browse", {"vault": vault})
    specs = next(item for item in browsed.get("items", []) if item.get("name") == "specs")
    assert specs.get("type") == "collection" and specs.get("doc_count") == 0

    repeated = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_collection",
        {"vault": vault, "path": "specs"},
    )
    assert repeated.get("created") is False

    normalized = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_collection",
        {"vault": vault, "path": "  /api-specs/  "},
    )
    assert normalized.get("collection", {}).get("path") == "api-specs"
    browsed = await _call_json(mcp_client, runtime_session, "akb_browse", {"vault": vault})
    assert any(item.get("name") == "api-specs" for item in browsed.get("items", []))

    for path in ("", "/", "../etc", "a/../b"):
        invalid = await _call_json(
            mcp_client,
            runtime_session,
            "akb_create_collection",
            {"vault": vault, "path": path},
            expect_error=True,
        )
        assert invalid.get("code") == "invalid_path"

    deleted_empty = await _call_json(
        mcp_client,
        runtime_session,
        "akb_delete_collection",
        {"vault": vault, "path": "specs"},
    )
    assert deleted_empty.get("ok") is True
    assert deleted_empty.get("deleted_docs") == 0
    assert deleted_empty.get("deleted_files") == 0
    browsed = await _call_json(mcp_client, runtime_session, "akb_browse", {"vault": vault})
    assert not any(item.get("name") == "specs" for item in browsed.get("items", []))

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_collection",
        {"vault": vault, "path": "docs"},
    )
    document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "docs", "title": "DocsDoc", "content": "## body"},
    )
    conflict = await _call_json(
        mcp_client,
        runtime_session,
        "akb_delete_collection",
        {"vault": vault, "path": "docs"},
        expect_error=True,
    )
    assert conflict.get("code") == "conflict"
    assert conflict.get("details", {}).get("doc_count", 0) >= 1
    still_exists = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": document["uri"]},
    )
    assert still_exists.get("title") == "DocsDoc"

    cascaded = await _call_json(
        mcp_client,
        runtime_session,
        "akb_delete_collection",
        {"vault": vault, "path": "docs", "recursive": True},
    )
    assert cascaded.get("ok") is True and cascaded.get("deleted_docs", 0) >= 1
    gone = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": document["uri"]},
        expect_error=True,
    )
    assert gone.get("code") == "not_found"

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_collection",
        {"vault": vault, "path": "keepempty"},
    )
    empty_document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "keepempty", "title": "OnlyDoc", "content": "## c"},
    )
    await _call_json(mcp_client, runtime_session, "akb_delete", {"uri": empty_document["uri"]})
    browsed = await _call_json(mcp_client, runtime_session, "akb_browse", {"vault": vault})
    keepempty = next(item for item in browsed.get("items", []) if item.get("name") == "keepempty")
    assert keepempty.get("doc_count") == 0

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_collection",
        {"vault": vault, "path": "nested/inner"},
    )
    nested_conflict = await _call_json(
        mcp_client,
        runtime_session,
        "akb_delete_collection",
        {"vault": vault, "path": "nested"},
        expect_error=True,
    )
    assert nested_conflict.get("code") == "conflict"
    assert nested_conflict.get("details", {}).get("sub_collection_count", 0) >= 1
    nested_deleted = await _call_json(
        mcp_client,
        runtime_session,
        "akb_delete_collection",
        {"vault": vault, "path": "nested", "recursive": True},
    )
    assert nested_deleted.get("ok") is True
    assert nested_deleted.get("deleted_sub_collections", 0) >= 1
    browsed = await _call_json(mcp_client, runtime_session, "akb_browse", {"vault": vault})
    assert not any(str(item.get("path", "")).startswith("nested") for item in browsed.get("items", []))


async def test_unicode_graph_grep_and_ownership(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    runtime_session: RuntimeContext,
) -> None:
    """Preserve Unicode search, graph edges, bounded grep replacement, and transfer."""

    vault = await _create_vault(mcp_client, runtime_session, "graph-detail")
    unicode_document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "한글컬렉션",
            "title": "제안요청서 분석 📄",
            "content": "# 한글 제목\n\n본문에 한글과 이모지 🎉 포함\n\n## 기술 요건\n- 가나다라\n- αβγδ\n- 中文テスト",
            "tags": ["한글", "테스트", "유니코드"],
        },
    )
    unicode_body = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": unicode_document["uri"]},
    )
    assert "가나다라" in unicode_body["content"]
    assert "中文" in unicode_body["content"]
    exact_unicode = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {"pattern": "가나다라", "vault": vault},
    )
    assert exact_unicode.get("total_matches", 0) >= 1
    semantic_unicode = await _search_until_found(
        mcp_client,
        runtime_session,
        "기술 요건 가나다라",
        vault=vault,
    )
    assert int(semantic_unicode.get("total", 0)) >= 1 or semantic_unicode.get("results")

    first = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "API Spec", "content": "# API Spec\nEndpoint definitions"},
    )
    second = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": vault, "collection": "specs", "title": "Data Model", "content": "# Data Model\nSchema definitions"},
    )
    linked = await _call_json(
        mcp_client,
        runtime_session,
        "akb_link",
        {"source": first["uri"], "target": second["uri"], "relation": "depends_on"},
    )
    assert linked.get("linked") or linked.get("created") or linked.get("edge_id")
    relations = await _call_json(
        mcp_client,
        runtime_session,
        "akb_relations",
        {"uri": first["uri"]},
    )
    assert _relation_count(relations) >= 1
    graph = await _call_json(
        mcp_client,
        runtime_session,
        "akb_graph",
        {"uri": first["uri"], "hops": 1},
    )
    assert len(graph.get("nodes", [])) >= 2
    assert len(graph.get("edges", [])) >= 1
    unlinked = await _call_json(
        mcp_client,
        runtime_session,
        "akb_unlink",
        {"source": first["uri"], "target": second["uri"], "relation": "depends_on"},
    )
    assert unlinked.get("unlinked", 0) >= 1 or unlinked.get("removed", 0) >= 1 or unlinked.get("deleted")
    relations_after = await _call_json(
        mcp_client,
        runtime_session,
        "akb_relations",
        {"uri": first["uri"]},
    )
    assert _relation_count(relations_after) == 0

    for index in range(1, 4):
        await _call_json(
            mcp_client,
            runtime_session,
            "akb_put",
            {
                "vault": vault,
                "collection": "replaceable",
                "title": f"Replace Doc {index}",
                "content": f"# Doc {index}\nThis uses OLD_PLACEHOLDER text.\nAnother line with OLD_PLACEHOLDER here.",
            },
        )
    budget = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {
            "pattern": "OLD_PLACEHOLDER",
            "vault": vault,
            "replace": "SHOULD_NOT_APPEAR",
            "limit": 1,
            "max_replacements": 2,
        },
        expect_error=True,
    )
    assert budget.get("code") == "bulk_too_large"
    assert budget.get("replacement_complete") is False
    assert budget.get("replacements") == []

    replacement = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {
            "pattern": "OLD_PLACEHOLDER",
            "vault": vault,
            "replace": "NEW_VALUE",
            "limit": 1,
            "max_replacements": 3,
        },
    )
    assert replacement.get("returned_docs") == 1
    assert replacement.get("total_docs") == 3
    assert replacement.get("truncated") is True
    assert replacement.get("replaced_docs") == 3
    assert replacement.get("replacement_complete") is True
    assert len(replacement.get("replacements", [])) == 3
    assert all(item.get("previous_commit") for item in replacement["replacements"])
    old_remaining = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {"pattern": "OLD_PLACEHOLDER", "vault": vault, "count_only": True},
    )
    new_found = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {"pattern": "NEW_VALUE", "vault": vault, "count_only": True},
    )
    assert old_remaining.get("total_matches") == 0
    assert new_found.get("total_matches", 0) >= 6

    regex_document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "replaceable",
            "title": "Regex Doc",
            "content": "# Regex\nVersion: v1.2.3\nRelease: v4.5.6",
        },
    )
    regex_replacement = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {
            "pattern": r"v(\d+)\.(\d+)\.(\d+)",
            "regex": True,
            "vault": vault,
            "collection": "replaceable",
            "replace": r"v\1.\2.99",
        },
    )
    assert regex_replacement.get("replaced_docs", 0) >= 1
    regex_body = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": regex_document["uri"]},
    )
    assert "v1.2.99" in regex_body["content"]
    assert "v4.5.99" in regex_body["content"]

    ownership_vault = await _create_vault(
        secondary_mcp_client.client,
        runtime_session,
        "ownership-detail",
    )
    primary_username = os.environ[runtime_session.descriptor.username_env]
    transferred = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_transfer_ownership",
        {"vault": ownership_vault, "new_owner": primary_username},
    )
    assert transferred.get("transferred") is True
    owner_write = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": ownership_vault,
            "collection": "owned",
            "title": "I own this now",
            "content": "# Mine",
        },
    )
    assert isinstance(owner_write.get("uri"), str)
    old_owner_transfer = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_transfer_ownership",
        {"vault": ownership_vault, "new_owner": secondary_mcp_client.username},
        expect_error=True,
    )
    assert "error" in old_owner_transfer or "denied" in str(old_owner_transfer).lower()
