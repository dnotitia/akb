"""Archive scope on the VAULT path (akb#530).

The vault path exists so an ordinary search filters by vault id instead of
enumerating candidate source ids. Archive scope used to disqualify it: the
scope defaults to `unarchived` and the gate demanded `all`, so the fast path
had no reachable caller and every default request fell back to enumeration —
which refuses with the bounded-corpus error on a large scope.

The predicate moved to hydration, where the AUTHORITATIVE status is already in
hand. These tests pin both halves: that the exclusion is exact, and that a
page shortened by it is refilled from the prefetch pool rather than returned
short.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.services import search_service as ss
from app.services.search_service import SearchService
from app.services.vector_store import VectorHit

pytestmark = pytest.mark.asyncio

VAULT_ID = uuid.UUID(int=7)


def _doc_row(doc_id: uuid.UUID, status: str) -> dict:
    return {
        "id": doc_id,
        "vault_name": "mine",
        "path": f"notes/{doc_id.int}.md",
        "title": f"Doc {doc_id.int}",
        "collection": "notes",
        "doc_type": "note",
        "summary": None,
        "tags": [],
        "status": status,
    }


class _Conn:
    """Answers the vault-path ACL lookup and the legacy-arm hydration join."""

    def __init__(self, statuses: dict[uuid.UUID, str]):
        self.statuses = statuses
        self.queries: list[str] = []

    async def fetchval(self, *_args):
        return False  # is_admin

    async def fetch(self, sql: str, *params):
        self.queries.append(sql)
        if "FROM vaults v WHERE" in sql or "FROM vaults WHERE name" in sql:
            return [{"id": VAULT_ID}]
        if "FROM documents d" in sql:
            wanted = {str(x) for x in params[0]}
            return [
                _doc_row(doc_id, status)
                for doc_id, status in self.statuses.items()
                if str(doc_id) in wanted
            ]
        return []


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _hit(doc_id: uuid.UUID, score: float = 1.0) -> VectorHit:
    return VectorHit(
        chunk_id=str(uuid.uuid4()),
        source_type="document",
        source_id=str(doc_id),
        section_path="",
        content="body",
        score=score,
    )


def _install(monkeypatch, conn):
    from app.config import settings
    from app.services import vault_backfill

    class _Capable:
        vault_filter_supported = True

    async def get_test_pool():
        return _Pool(conn)

    monkeypatch.setattr(settings, "vault_filter_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rerank_enabled", False, raising=False)
    monkeypatch.setattr(ss, "get_vector_store", lambda: _Capable())
    monkeypatch.setattr(vault_backfill, "is_ready", lambda: True)
    monkeypatch.setattr(ss, "get_pool", get_test_pool)
    monkeypatch.setattr(ss, "generate_embeddings", AsyncMock(return_value=[[0.1]]))
    monkeypatch.setattr(ss, "_configured_document_source_type", lambda: "document")


@pytest.mark.parametrize("scope,kept", [
    ("unarchived", False),
    ("all", True),
])
async def test_hydration_applies_archive_scope_to_the_authoritative_status(monkeypatch, scope, kept):
    """`unarchived` excludes an archived document and says so by cause;
    `all` keeps it. The status read is the arm's authority — `documents.status`
    here, verified frontmatter on the native arm."""
    doc_id = uuid.uuid4()
    conn = _Conn({doc_id: "archived"})
    _install(monkeypatch, conn)

    results, dropped = await SearchService()._hydrate_hits([_hit(doc_id)], archive_scope=scope)

    assert len(results) == (1 if kept else 0)
    assert dropped == ({} if kept else {"archive_scope_excluded": 1})


async def test_hydration_defaults_to_no_archive_filtering(monkeypatch):
    """The default keeps every caller that does not pass a scope unchanged —
    the id path already applied the predicate during candidate selection."""
    doc_id = uuid.uuid4()
    conn = _Conn({doc_id: "archived"})
    _install(monkeypatch, conn)

    results, dropped = await SearchService()._hydrate_hits([_hit(doc_id)])

    assert len(results) == 1
    assert dropped == {}


async def test_a_page_shortened_by_the_archive_filter_is_refilled(monkeypatch):
    """An archived hit inside the page must not cost a result slot: the search
    refills from the rest of the deduped prefetch pool. Without the refill this
    returns 2 of the 3 requested."""
    ids = [uuid.uuid4() for _ in range(6)]
    # The second hit of the page is archived; the pool has spare candidates.
    statuses = {doc_id: ("archived" if i == 1 else "draft") for i, doc_id in enumerate(ids)}
    conn = _Conn(statuses)
    _install(monkeypatch, conn)
    # search_prefetch gives the pool headroom beyond `limit`; with none there
    # is nothing to refill from and the page is legitimately short.
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 6, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=3)

    assert response.returned == 3
    assert [r.status for r in response.results] == ["draft", "draft", "draft"]
    # Pool order is preserved and the archived hit at index 1 is replaced by
    # the next spare candidate (index 3), not by shortening the page.
    assert [r.title for r in response.results] == [
        f"Doc {ids[0].int}", f"Doc {ids[2].int}", f"Doc {ids[3].int}",
    ]
    assert response.degradation_reason == "hydration_dropped:archive_scope_excluded=1"


async def test_an_exhausted_pool_returns_a_short_page_that_names_its_cause(monkeypatch):
    """With no spare candidates the page really is short. `returned < limit`
    then has one stated cause instead of two unstated ones — the drop
    accounting is what makes hydration-time filtering readable."""
    ids = [uuid.uuid4() for _ in range(2)]
    conn = _Conn({ids[0]: "draft", ids[1]: "archived"})
    _install(monkeypatch, conn)
    from app.config import settings
    monkeypatch.setattr(settings, "search_prefetch", 0, raising=False)

    service = SearchService()
    monkeypatch.setattr(
        service, "_run_vector_search",
        AsyncMock(return_value=([_hit(d, score=1.0 - i / 10) for i, d in enumerate(ids)], None)),
    )

    response = await service.search("x", vault="mine", user_id=str(uuid.UUID(int=1)), limit=2)

    assert response.returned == 1
    assert response.degraded is True
    assert response.degradation_reason == "hydration_dropped:archive_scope_excluded=1"


async def test_tables_and_files_carry_no_status_and_survive_the_default_scope(monkeypatch):
    """Only documents have an archived state. `status_matches(None, …)` keeps
    table/file rows under `unarchived` and `all`, matching what the candidate
    branches do with their `scope != "archived"` guards."""
    from app.services.search_filters import status_matches

    assert status_matches(None, "unarchived") is True
    assert status_matches(None, "all") is True
    assert status_matches(None, "archived") is False
