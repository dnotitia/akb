"""Focused compatibility regressions for an existing-database Native cutover."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_backend import NativeRevisionBackend


def _pool(*, row=None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    return pool, conn


@pytest.mark.asyncio
async def test_migrated_native_document_fills_missing_public_metadata_from_frozen_legacy_row():
    created_at = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
    updated_at = datetime(2026, 9, 3, 2, 3, 4, tzinfo=UTC)
    pool, conn = _pool(
        row={
            "title": "Collector fixture",
            "doc_type": "reference",
            "status": "active",
            "summary": "A to B source",
            "domain": "native-revision",
            "created_by": None,
            "created_at": created_at,
            "updated_at": updated_at,
            "tags": ["fixture", "collector"],
            "metadata": {},
        }
    )
    service = NativeDocumentService(pool=pool)
    snapshot = SimpleNamespace(
        resource_id=uuid.uuid4(),
        text=(
            "---\ntitle: Collector fixture\ntype: reference\n"
            "tags: [fixture, collector]\nsummary: A to B source\n"
            "domain: native-revision\n---\nbody\n"
        ),
    )

    frontmatter, body = await service._document_frontmatter(uuid.uuid4(), snapshot)

    assert body == "body"
    assert frontmatter["status"] == "active"
    assert frontmatter["created_at"] == created_at.isoformat()
    assert frontmatter["updated_at"] == updated_at.isoformat()
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_native_frontmatter_never_reads_legacy_document_projection():
    pool, conn = _pool(row=None)
    service = NativeDocumentService(pool=pool)
    snapshot = SimpleNamespace(
        resource_id=uuid.uuid4(),
        text=(
            "---\ntitle: Native\ntype: note\nstatus: active\n"
            "created_at: '2026-09-03T01:02:03+00:00'\n"
            "updated_at: '2026-09-03T01:02:03+00:00'\n"
            "tags: []\n---\nbody\n"
        ),
    )

    frontmatter, body = await service._document_frontmatter(uuid.uuid4(), snapshot)

    assert frontmatter["status"] == "active"
    assert body == "body"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_migrated_status_uses_frozen_legacy_status():
    pool, conn = _pool(
        row={
            "title": "Imported",
            "doc_type": "reference",
            "status": "active",
            "summary": None,
            "domain": None,
            "created_by": None,
            "created_at": None,
            "updated_at": None,
            "tags": [],
        }
    )
    service = NativeDocumentService(pool=pool)
    snapshot = SimpleNamespace(
        resource_id=uuid.uuid4(),
        text=(
            "---\ntitle: Imported\ntype: reference\nstatus:\n"
            "created_at: '2026-09-03T01:02:03+00:00'\n"
            "updated_at: '2026-09-03T01:02:03+00:00'\n"
            "tags: []\n---\nbody\n"
        ),
    )

    frontmatter, _ = await service._document_frontmatter(uuid.uuid4(), snapshot)

    assert frontmatter["status"] == "active"
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_history_annotates_mapped_and_native_authors(monkeypatch):
    pool, _ = _pool()
    backend = NativeRevisionBackend(pool=pool)
    resolver = AsyncMock(
        return_value={
            "owner": "Fixture Owner",
            "11111111-1111-1111-1111-111111111111": "Fixture Reader",
        }
    )
    monkeypatch.setattr(
        "app.services.native_revision_backend.resolve_display_names",
        resolver,
    )
    entries = [
        {"hash": "native", "author": "owner"},
        {"hash": "legacy", "author": "11111111-1111-1111-1111-111111111111"},
        {"hash": "external", "author": "external-committer"},
    ]

    annotated = await backend._annotate_history_authors(entries)

    assert annotated[0]["author_name"] == "Fixture Owner"
    assert annotated[1]["author_name"] == "Fixture Reader"
    assert "author_name" not in annotated[2]
    resolver.assert_awaited_once_with(
        ["owner", "11111111-1111-1111-1111-111111111111", "external-committer"],
        pool=pool,
    )
