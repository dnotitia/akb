"""Unit tests for DocumentService.history() and its author annotation.

history() is the single source of truth behind both the akb_history MCP
tool and GET /api/v1/history/{vault}/{doc}. These tests pin the bits that
are easy to regress without a live DB/git: the created_at lineage boundary,
the NotFoundError surface, and the id-OR-username → display_name resolver.
Full git integration is covered by the e2e suites.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.exceptions import NotFoundError
from app.services.document_service import DocumentService


def _service_with_repos(*, vault_id, doc_row):
    """A DocumentService whose _repos() returns mocked vault/doc repos."""
    svc = DocumentService(git=MagicMock())
    vault_repo = MagicMock()
    vault_repo.get_id_by_name = AsyncMock(return_value=vault_id)
    doc_repo = MagicMock()
    doc_repo.find_by_ref = AsyncMock(return_value=doc_row)
    svc._repos = AsyncMock(return_value=(vault_repo, doc_repo, MagicMock()))
    return svc


def _patch_pool(monkeypatch, *, fetch_rows):
    """Patch document_service.get_pool so a `async with pool.acquire()` block
    yields a conn whose .fetch(...) returns `fetch_rows`."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_rows)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    monkeypatch.setattr(
        "app.services.document_service.get_pool", AsyncMock(return_value=pool)
    )
    return conn


@pytest.mark.asyncio
async def test_history_missing_vault_raises_not_found():
    svc = DocumentService(git=MagicMock())
    vault_repo = MagicMock()
    vault_repo.get_id_by_name = AsyncMock(return_value=None)
    svc._repos = AsyncMock(return_value=(vault_repo, MagicMock(), MagicMock()))

    with pytest.raises(NotFoundError):
        await svc.history("ghost-vault", "doc.md")


@pytest.mark.asyncio
async def test_history_missing_doc_raises_not_found():
    svc = _service_with_repos(vault_id="v1", doc_row=None)

    with pytest.raises(NotFoundError):
        await svc.history("v", "nope.md")


@pytest.mark.asyncio
async def test_history_passes_created_at_as_lineage_boundary(monkeypatch):
    """created_at must reach git.file_log as Unix seconds so a re-created
    path doesn't inherit the prior document's commits."""
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    doc_row = {
        "path": "notes/a.md",
        "vault_name": "v",
        "created_at": created,
    }
    svc = _service_with_repos(vault_id="v1", doc_row=doc_row)
    svc.git.file_log = MagicMock(return_value=[])
    _patch_pool(monkeypatch, fetch_rows=[])

    out = await svc.history("v", "notes/a.md", limit=7)

    assert out["uri"].endswith("/doc/a.md")
    svc.git.file_log.assert_called_once()
    _, kwargs = svc.git.file_log.call_args
    assert kwargs["max_count"] == 7
    assert kwargs["since_epoch"] == int(created.timestamp())


@pytest.mark.asyncio
async def test_history_null_created_at_no_boundary(monkeypatch):
    doc_row = {"path": "a.md", "vault_name": "v", "created_at": None}
    svc = _service_with_repos(vault_id="v1", doc_row=doc_row)
    svc.git.file_log = MagicMock(return_value=[])
    _patch_pool(monkeypatch, fetch_rows=[])

    await svc.history("v", "a.md")

    _, kwargs = svc.git.file_log.call_args
    assert kwargs["since_epoch"] is None


@pytest.mark.asyncio
async def test_annotate_authors_resolves_username_and_uuid(monkeypatch):
    """The git author is normally the actor's username; legacy rows may
    carry a UUID. Both forms resolve to display_name in one query; an
    unknown author keeps only its raw value."""
    entries = [
        {"hash": "a", "author": "younglo_kim"},
        {"hash": "b", "author": "11111111-1111-1111-1111-111111111111"},
        {"hash": "c", "author": "external-committer"},
    ]
    rows = [
        {"id": "00000000-0000-0000-0000-000000000000",
         "username": "younglo_kim", "name": "Younglo Kim"},
        {"id": "11111111-1111-1111-1111-111111111111",
         "username": "alice", "name": "Alice A"},
    ]
    svc = DocumentService(git=MagicMock())
    _patch_pool(monkeypatch, fetch_rows=rows)

    out = await svc._annotate_history_authors(entries)

    by_hash = {e["hash"]: e for e in out}
    assert by_hash["a"]["author_name"] == "Younglo Kim"   # by username
    assert by_hash["b"]["author_name"] == "Alice A"       # by UUID
    assert "author_name" not in by_hash["c"]              # unresolved


@pytest.mark.asyncio
async def test_annotate_authors_empty_skips_query(monkeypatch):
    conn = _patch_pool(monkeypatch, fetch_rows=[])
    svc = DocumentService(git=MagicMock())

    out = await svc._annotate_history_authors([])

    assert out == []
    conn.fetch.assert_not_called()
