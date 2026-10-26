"""Real-PG graph catalog regressions; uses isolated disposable databases.

AKB_TEST_DSN / REQUIRE_REAL_PG follow the existing native graph test lane.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.services import document_counters, kg_service
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services.native_revision_service import NativeRevisionService
from app.services.m1_pg_body_store import M1PgBodyStore
from tests.concurrency.test_native_derived_pipeline import _fresh_database
from tests.test_native_graph_resource_identity_pg import _apply_migration, _args, _vault

pytestmark = pytest.mark.asyncio


def _body(title, status="active"):
    return ("---\ntitle: " + json.dumps(title) + "\ntype: note\nstatus: " + status
            + "\ncreated_by: review\ncreated_at: '2026-10-16T00:00:00Z'"
            + "\nupdated_at: '2026-10-16T00:00:00Z'\ntags: []\n---\nGraph body\n")


async def _setup(pool, monkeypatch):
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: True)

    async def get_pool():
        return pool

    monkeypatch.setattr(kg_service, "get_pool", get_pool)
    return await _vault(pool), NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))


async def test_native_graph_metadata_uses_live_heads_and_same_migration_fallback(monkeypatch):
    async with _fresh_database() as pool:
        vault, native = await _setup(pool, monkeypatch)
        live = await native.create_text(**_args(vault, "live.md"), payload=_body("Current native title"))
        migrated = await native.create_text(**_args(vault, "migrated.md"), payload="body without frontmatter")
        deleted = await native.create_text(**_args(vault, "deleted.md"), payload=_body("Deleted"))
        await native.delete_resource(**_args(vault, "deleted.md"), expected_revision_id=deleted.revision_id)
        async with pool.acquire() as conn:
            # Retained stale projections must neither hide current native
            # metadata nor resurrect a deleted/legacy-only document.
            for rid, path, title in ((live.resource_id, "live.md", "Stale title"),
                                     (migrated.resource_id, "migrated.md", "Migrated archived"),
                                     (deleted.resource_id, "deleted.md", "Zombie"),
                                     (uuid.uuid4(), "legacy-only.md", "Not native")):
                await conn.execute("INSERT INTO documents(id,vault_id,path,title,status) VALUES($1,$2,$3,$4,'archived')",
                                   rid, vault, path, title)
            rows = await kg_service._graph_document_rows(conn, vault)
            assert rows == [{"path": "live.md", "title": "Current native title"}]
            rows = await kg_service._graph_document_rows(conn, vault, include_archived=True)
            assert {row["path"]: row["title"] for row in rows} == {
                "live.md": "Current native title", "migrated.md": "Migrated archived",
            }
            assert await kg_service._graph_document_rows(conn, vault, paths=[]) == []
            assert await kg_service._graph_document_rows(conn, vault, excluded_paths=["live.md"]) == []


async def test_native_pages_progress_past_archives_and_share_one_snapshot(monkeypatch):
    async with _fresh_database() as pool:
        vault, native = await _setup(pool, monkeypatch)
        oldest = await native.create_text(**_args(vault, "oldest.md"), payload=_body("Before concurrent edit"))
        for i in range(256):
            await native.create_text(**_args(vault, f"archived-{i}.md"), payload=_body("Retired", "archived"))

        original = NativeRevisionRepository.list_live_heads_with_payloads
        pages = []

        async def observed(self, **kwargs):
            assert kwargs["conn"].is_in_transaction()
            pages.append(kwargs)
            rows = await original(self, **kwargs)
            if len(pages) == 1:
                # This unseen resource moves ahead of a read-committed
                # keyset cursor. Repeatable-read must still find its old Head.
                await native.replace_text(**_args(vault, "oldest.md"), payload=_body("After concurrent edit"),
                                          expected_revision_id=oldest.revision_id)
            return rows

        monkeypatch.setattr(NativeRevisionRepository, "list_live_heads_with_payloads", observed)
        async with pool.acquire() as conn:
            rows = await kg_service._graph_document_rows(conn, vault, limit=1)
        assert rows == [{"path": "oldest.md", "title": "Before concurrent edit"}]
        assert len(pages) == 2
        assert pages[0]["limit"] == pages[1]["limit"] == 256
        assert pages[1]["before"] is not None
        current = await native.get_current_reference(namespace_id=vault, surface="document", reference="oldest.md")
        assert "After concurrent edit" in current.text


async def test_provenance_uses_native_identity_not_legacy_and_excludes_deleted_or_foreign(monkeypatch):
    async with _fresh_database() as pool:
        vault, native = await _setup(pool, monkeypatch)
        await _apply_migration(pool, 106)
        other_vault = await _vault(pool, "other")
        old = await native.create_text(**_args(vault, "a.md"), payload=_body("Native A"))
        result = await kg_service.get_provenance("a.md", vault_id=vault)
        assert result["doc_id"] == str(old.resource_id)
        assert result["title"] == "Native A"
        assert result["current_commit"] == old.revision_id
        assert (await kg_service.get_provenance(str(old.resource_id), vault_id=other_vault))["code"] == "not_found"
        await native.delete_resource(**_args(vault, "a.md"), expected_revision_id=old.revision_id)
        replacement = await native.create_text(**_args(vault, "a.md"), payload=_body("Replacement A"))
        assert (await kg_service.get_provenance(str(old.resource_id), vault_id=vault))["code"] == "not_found"
        assert (await kg_service.get_provenance("a.md", vault_id=vault))["doc_id"] == str(replacement.resource_id)


async def test_legacy_catalog_remains_the_authority_in_legacy_mode(monkeypatch):
    async with _fresh_database() as pool:
        vault, native = await _setup(pool, monkeypatch)
        monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: False)
        await native.create_text(**_args(vault, "native-only.md"), payload=_body("Not selected"))
        async with pool.acquire() as conn:
            rid = await conn.fetchval("INSERT INTO documents(vault_id,path,title) VALUES($1,'a.md','Legacy A') RETURNING id", vault)
            assert await kg_service._graph_document_rows(conn, vault) == [{"path": "a.md", "title": "Legacy A"}]
        result = await kg_service.get_provenance("a.md", vault_id=vault)
        assert result["doc_id"] == str(rid)
        assert result["title"] == "Legacy A"


async def test_native_rest_and_mcp_reads_enforce_real_vault_acl_without_a_legacy_mirror(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.api.deps import get_current_user
    from app.api.routes import documents, knowledge
    from app.db import postgres
    from app.exceptions import AKBError, ForbiddenError
    from app.main import akb_error_handler
    from app.services.native_document_service import NativeDocumentService
    import mcp_server.server as mcp

    async with _fresh_database() as pool:
        vault, native = await _setup(pool, monkeypatch)
        await _apply_migration(pool, 106)
        monkeypatch.setattr(postgres, "_pool", pool)
        monkeypatch.setattr(documents, "doc_service", NativeDocumentService(pool=pool))
        created = await native.create_text(**_args(vault, "canary.md"), payload=_body("Native API canary"))
        reader_vault = await _vault(pool, "reader")
        outsider_vault = await _vault(pool, "outsider")
        async with pool.acquire() as conn:
            owner = await conn.fetchval("SELECT owner_id FROM vaults WHERE id=$1", vault)
            reader = await conn.fetchval("SELECT owner_id FROM vaults WHERE id=$1", reader_vault)
            outsider = await conn.fetchval("SELECT owner_id FROM vaults WHERE id=$1", outsider_vault)
            await conn.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'reader')", vault, reader)
            assert await conn.fetchval("SELECT count(*) FROM documents WHERE vault_id=$1", vault) == 0
        app = FastAPI()
        app.include_router(documents.router, prefix="/api/v1")
        app.include_router(knowledge.router, prefix="/api/v1")
        app.add_exception_handler(AKBError, akb_error_handler)
        uri = "akb://review/doc/canary.md"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for uid, expected in ((owner, 200), (reader, 200), (outsider, 403)):
                app.dependency_overrides[get_current_user] = lambda uid=uid: SimpleNamespace(user_id=str(uid))
                response = await client.get("/api/v1/documents/review/canary.md")
                assert response.status_code == expected, response.text
                for route, params in (("provenance", {"uri": uri}),
                                      ("graph/overview", {"vault": "review"}),
                                      ("graph/health", {"vault": "review"})):
                    response = await client.get("/api/v1/" + route, params=params)
                    assert response.status_code == expected, response.text
                    if expected == 200 and route == "provenance":
                        assert response.json()["doc_id"] == str(created.resource_id)
                        assert response.json()["current_commit"] == created.revision_id
                    elif expected == 200 and route == "graph/overview":
                        assert response.json()["nodes"] == [{
                            "uri": uri, "name": "Native API canary", "resource_type": "doc", "degree": 0,
                        }]
                    elif expected == 200 and route == "graph/health":
                        assert response.json()["orphans"]["count"] == 1
                user = mcp._MCPUser(user_id=str(uid))
                if expected == 403:
                    with pytest.raises(ForbiddenError):
                        await mcp._handle_provenance({"uri": uri}, str(uid), user)
                else:
                    result = await mcp._handle_provenance({"uri": uri}, str(uid), user)
                    assert result["doc_id"] == str(created.resource_id)
                    assert result["title"] == "Native API canary"
