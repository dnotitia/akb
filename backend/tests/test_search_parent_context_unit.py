from __future__ import annotations

import uuid

import pytest

from app.services import search_service
from app.services.search_service import SearchService
from app.services.vector_store import VectorHit

pytestmark = pytest.mark.asyncio


class _ContextConn:
    def __init__(self, doc_id: uuid.UUID):
        self.doc_id = doc_id
        self.queries: list[str] = []

    async def fetch(self, sql: str, *_params):
        self.queries.append(sql)
        if "FROM documents d" in sql:
            return [{
                "id": self.doc_id,
                "vault_name": "reef-akb",
                "path": "research/catalog.md",
                "title": "MCP Tool Catalog",
                "collection": "research",
                "doc_type": "report",
                "summary": "Document-level summary",
                "tags": ["topic:mcp"],
            }]
        if "c.path = ANY" in sql:
            return [{
                "vault_name": "reef-akb",
                "vault_description": "AKB project knowledge workspace",
                "collection_path": "research",
                "collection_summary": "Technical comparisons and AKB recommendations",
            }]
        return []


class _Acquire:
    def __init__(self, conn: _ContextConn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, conn: _ContextConn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


async def test_search_hydration_adds_parent_context_without_changing_score(monkeypatch):
    doc_id = uuid.uuid4()
    conn = _ContextConn(doc_id)

    async def get_test_pool():
        return _Pool(conn)

    monkeypatch.setattr(search_service, "get_pool", get_test_pool)
    monkeypatch.setattr(search_service, "_configured_document_source_type", lambda: "document")

    results = await SearchService()._hydrate_hits([
        VectorHit(
            chunk_id=str(uuid.uuid4()),
            source_type="document",
            source_id=str(doc_id),
            section_path="",
            content="matching section",
            score=0.82,
        )
    ])

    assert len(results) == 1
    assert results[0].collection == "research"
    assert results[0].collection_summary == "Technical comparisons and AKB recommendations"
    assert results[0].vault_description == "AKB project knowledge workspace"
    assert results[0].score == 0.82
    assert all("INSERT" not in sql and "UPDATE" not in sql for sql in conn.queries)
