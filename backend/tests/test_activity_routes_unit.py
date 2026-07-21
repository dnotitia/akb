"""Serialization contracts for activity, recent, history, and diff routes."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.activity import (
    AkbActivityEnvelope,
    AkbDocumentDiffEnvelope,
    AkbDocumentHistoryEnvelope,
    AkbRecentChangesEnvelope,
)


@pytest.fixture
def routes(monkeypatch, tmp_path):
    """Import the module only after redirecting its module-level GitService."""
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.api.routes import activity

    return activity


def _pool_with(*, rows=(), vault_id="vault-id"):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.fetchrow = AsyncMock(return_value={"id": vault_id})

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    return pool, conn


@pytest.mark.asyncio
async def test_activity_adds_kind_and_preserves_unresolved_author_absence(monkeypatch, routes):
    entry = {
        "hash": "abc123",
        "subject": "[update] notes/a.md",
        "author": "external-author",
        "date": "2026-07-21T00:00:00+00:00",
        "action": "update",
        "summary": "changed",
        "agent": "external-author",
        "files": [{"path": "notes/a.md", "change": "modified"}],
    }
    monkeypatch.setattr(routes, "check_vault_access", AsyncMock())
    monkeypatch.setattr(routes.git, "vault_log", MagicMock(return_value=[entry]))
    monkeypatch.setattr(routes, "_resolve_activity_authors", AsyncMock(return_value=[entry]))

    out = await routes.vault_activity(
        "v", collection=None, author=None, since=None, limit=20, user=MagicMock(),
    )

    assert out == {"kind": "activity", "vault": "v", "total": 1, "activity": [entry]}
    dumped = AkbActivityEnvelope.model_validate(out).model_dump(exclude_unset=True)
    assert "author_name" not in dumped["activity"][0]


@pytest.mark.asyncio
async def test_recent_adds_kind_and_preserves_explicit_nulls(monkeypatch, routes):
    rows = [{
        "id": "uuid-1",
        "title": "Nullable",
        "path": "notes/nullable.md",
        "doc_type": None,
        "current_commit": None,
        "updated_at": None,
        "vault_name": "v",
        "metadata": {"id": "d-nullable"},
    }]
    pool, _ = _pool_with(rows=rows)
    monkeypatch.setattr(routes, "get_pool", AsyncMock(return_value=pool))

    out = await routes.recent_changes(vault=None, limit=20, user=MagicMock(user_id="u"))

    assert out["kind"] == "recent_changes"
    assert out["changes"][0]["commit"] is None
    assert out["changes"][0]["changed_at"] is None
    dumped = AkbRecentChangesEnvelope.model_validate(out).model_dump(exclude_unset=True)
    assert dumped["changes"][0]["commit"] is None
    assert dumped["changes"][0]["changed_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("git_result", "has_error"),
    [
        ({"file": "a.md", "commit": "good", "type": "modified", "diff": "@@"}, False),
        ({
            "file": "a.md", "commit": "bad", "type": "unknown", "diff": "",
            "error": "commit not found",
        }, True),
    ],
)
async def test_diff_adds_kind_and_preserves_optional_error(
    monkeypatch, routes, git_result, has_error,
):
    pool, _ = _pool_with()
    monkeypatch.setattr(routes, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(routes, "check_vault_access", AsyncMock())
    monkeypatch.setattr(
        routes.DocumentRepository,
        "find_by_ref_with_conn",
        AsyncMock(return_value={"path": "a.md"}),
    )
    monkeypatch.setattr(routes.git, "file_diff", MagicMock(return_value=git_result))

    out = await routes.document_diff("v", "a.md", commit=git_result["commit"], user=MagicMock())

    assert out["kind"] == "document_diff"
    dumped = AkbDocumentDiffEnvelope.model_validate(out).model_dump(exclude_unset=True)
    assert ("error" in dumped) is has_error
    assert dumped["type"] == git_result["type"]


@pytest.mark.asyncio
async def test_history_adds_kind_without_changing_entries(monkeypatch, routes):
    entry = {
        "hash": "abc123",
        "message": "update",
        "author": "writer",
        "date": datetime(2026, 7, 21, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(routes, "check_vault_access", AsyncMock())
    monkeypatch.setattr(
        routes.doc_service,
        "history",
        AsyncMock(return_value={"uri": "akb://v/doc/a.md", "history": [entry]}),
    )

    out = await routes.document_history("v", "a.md", limit=20, user=MagicMock())

    assert out == {
        "kind": "document_history",
        "uri": "akb://v/doc/a.md",
        "history": [entry],
    }
    AkbDocumentHistoryEnvelope.model_validate(out)
