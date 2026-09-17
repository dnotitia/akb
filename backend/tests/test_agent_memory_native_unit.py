"""Native-ledger reads for agent memory recall (#542).

On `postgres_native` the native path never writes `documents`, so the three
`FROM documents` reads in this service return only pre-cutover rows — a
silently partial answer that grows one write at a time (the cutover projects
everything once, so the table stays plausible). These pin the dispatched
reads: the native ledger answers on a native installation, the legacy catalog
otherwise. The authority selector is #525's counter module, not a local
branch. No live DB — pool, selector, and the native document service are
patched; what is asserted is which statement/shape each read uses.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services import agent_memory_service


def _patch(monkeypatch, *, native: bool, fetch_rows=None, fetchrow=None):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=list(fetch_rows or []))
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetchval = AsyncMock(return_value=0)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    monkeypatch.setattr(
        "app.services.agent_memory_service.get_pool", AsyncMock(return_value=pool)
    )
    monkeypatch.setattr(
        "app.services.document_counters.native_documents_are_authoritative",
        lambda: native,
    )
    return conn


def _doc(namespace="v", path="preferences/a.md"):
    return SimpleNamespace(
        path=path,
        title="T",
        summary="S",
        type="note",
        tags=[],
        updated_at=None,
    )


async def test_native_scope_read_hits_the_ledger_not_the_catalog(monkeypatch):
    conn = _patch(
        monkeypatch,
        native=True,
        fetchrow={"id": uuid.uuid4()},
        fetch_rows=[{"resource_id": uuid.uuid4(), "path": "preferences/a.md"}],
    )

    class _Svc:
        async def get_by_resource_id(self, vault, rid):
            return _doc()

    monkeypatch.setattr(
        "app.services.agent_memory_service.NativeDocumentService", lambda: _Svc()
    )
    # Bound locally in _fetch_scope_native; patch at the util source.
    monkeypatch.setattr(
        "app.util.text.like_escape", lambda s: s.replace("%", "\\%"),
    )

    svc = agent_memory_service.AgentMemoryService(doc_service=MagicMock())
    out = await svc._fetch_scope("v", "preferences", None, 5)

    assert len(out) == 1
    assert out[0]["title"] == "T"
    statements = [" ".join(c.args[0].split()) for c in conn.fetch.await_args_list]
    assert any("FROM native_resources" in s for s in statements)
    assert not any("FROM documents" in s for s in statements)


async def test_legacy_scope_read_keeps_the_catalog(monkeypatch):
    conn = _patch(
        monkeypatch,
        native=False,
        fetch_rows=[{
            "id": uuid.uuid4(), "title": "T", "path": "preferences/a.md",
            "summary": "S", "updated_at": None, "current_commit": "c",
            "doc_type": "note", "tags": [],
        }],
    )

    svc = agent_memory_service.AgentMemoryService(doc_service=MagicMock())
    out = await svc._fetch_scope("v", "preferences", None, 5)

    assert len(out) == 1
    assert out[0]["title"] == "T"
    statements = [" ".join(c.args[0].split()) for c in conn.fetch.await_args_list]
    assert any("FROM documents" in s for s in statements)
    assert not any("FROM native_resources" in s for s in statements)


async def test_native_recap_resolves_through_the_native_service(monkeypatch):
    conn = _patch(
        monkeypatch, native=True, fetchrow={"id": uuid.uuid4(), "resource_id": uuid.uuid4()},
    )

    seen = {}

    class _Svc:
        async def get_by_resource_id(self, vault, rid):
            seen["rid"] = rid
            return _doc(path="sessions/2026-09-17/x/y/recap.md")

    monkeypatch.setattr(
        "app.services.agent_memory_service.NativeDocumentService", lambda: _Svc()
    )

    svc = agent_memory_service.AgentMemoryService(doc_service=MagicMock())
    out = await svc._fetch_recap_summary("v", "sessions/2026-09-17/x/y")

    assert out is not None
    assert out["uri"].endswith("/doc/recap.md")
    assert seen["rid"] == conn.fetchrow.return_value["resource_id"]


async def test_native_snapshot_count_reads_the_ledger(monkeypatch):
    conn = _patch(monkeypatch, native=True)

    svc = agent_memory_service.AgentMemoryService(doc_service=MagicMock())
    await svc._count_snapshots("v", "sessions/2026-09-17/x/y")

    statements = [" ".join(c.args[0].split()) for c in conn.fetchval.await_args_list]
    assert any("FROM native_resources" in s for s in statements)
