"""Stable Resource publication discovery and identity-pinned native reads."""

import sqlite3
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.exceptions import NotFoundError
from app.services.document_service import newest_public_slug
from app.services.native_document_service import NativeDocumentService


class _Connection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("""CREATE TABLE publications (
            slug TEXT, vault_id TEXT, resource_type TEXT, document_id TEXT,
            native_document_id TEXT, resource_uri TEXT, created_at INTEGER
        )""")

    def add(self, slug, vault, *, native=None, legacy=None, uri="akb://v/docs/current.md", age=1):
        self.db.execute("INSERT INTO publications VALUES (?, ?, 'document', ?, ?, ?, ?)",
                        (slug, str(vault), str(legacy) if legacy else None,
                         str(native) if native else None, uri, age))

    async def fetchval(self, query, *args):
        # Execute the real selection logic; only remove PostgreSQL's UUID cast.
        row = self.db.execute(query.replace("::uuid", ""),
                              {str(i): str(arg) if isinstance(arg, uuid.UUID) else arg
                               for i, arg in enumerate(args, 1)}).fetchone()
        return row[0] if row else None


@pytest.mark.asyncio
async def test_native_discovery_prefers_identity_across_move_and_excludes_reused_path():
    conn = _Connection()
    vault, resource, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.add("old-stable", vault, native=resource, uri="akb://v/docs/old.md")
    conn.add("new-stable", vault, native=resource, uri="akb://v/docs/old.md", age=2)
    conn.add("fallback", vault, age=99)
    conn.add("reused", vault, native=other, age=100)
    conn.add("other-vault", uuid.uuid4(), native=resource, age=101)
    assert await newest_public_slug(
        conn, vault_id=vault, document_id=None, native_document_id=resource,
        resource_uri="akb://v/docs/current.md",
    ) == "new-stable"
    assert await newest_public_slug(
        conn, vault_id=vault, document_id=None, native_document_id=uuid.uuid4(),
        resource_uri="akb://v/docs/old.md",
    ) is None


@pytest.mark.asyncio
async def test_uri_fallback_is_legacy_only_and_rejects_bound_rows():
    conn = _Connection()
    vault = uuid.uuid4()
    conn.add("native-bound", vault, native=uuid.uuid4(), age=3)
    conn.add("legacy-bound", vault, legacy=uuid.uuid4(), age=2)
    for native in (None, uuid.uuid4()):
        assert await newest_public_slug(
            conn, vault_id=vault, document_id=None, native_document_id=native,
            resource_uri="akb://v/docs/current.md",
        ) is None
    conn.add("old-unbound", vault)
    assert await newest_public_slug(
        conn, vault_id=vault, document_id=None, native_document_id=uuid.uuid4(),
        resource_uri="akb://v/docs/current.md",
    ) is None
    assert await newest_public_slug(
        conn, vault_id=vault, document_id=None,
        resource_uri="akb://v/docs/current.md",
    ) == "old-unbound"


@pytest.mark.asyncio
async def test_legacy_call_preserves_document_identity_lookup():
    conn = _Connection()
    vault, document = uuid.uuid4(), uuid.uuid4()
    conn.add("legacy", vault, legacy=document)
    assert await newest_public_slug(
        conn, vault_id=vault, document_id=document, resource_uri="akb://v/docs/moved.md",
    ) == "legacy"


@pytest.mark.asyncio
async def test_native_response_passes_current_resource_identity(monkeypatch):
    service = NativeDocumentService(pool=object())
    vault, resource = uuid.uuid4(), uuid.uuid4()
    current = SimpleNamespace(
        resource_id=resource, path="moved.md", text="body", revision_id="a" * 40,
        resource_created_at=datetime.now(UTC), resource_updated_at=datetime.now(UTC),
    )
    monkeypatch.setattr(service, "_document_frontmatter", AsyncMock(return_value=({}, None)))
    monkeypatch.setattr(service, "_created_by_name", AsyncMock(return_value=None))
    slug = AsyncMock(return_value="published")
    monkeypatch.setattr(service, "_public_slug", slug)
    response = await service._response(vault="v", vault_id=vault, current=current)
    slug.assert_awaited_once_with(vault, "v", "moved.md", native_document_id=resource)
    assert response.public_slug == "published"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_native_publication_read_uses_exact_resource_without_path_fallback(monkeypatch, missing):
    service = NativeDocumentService(pool=object())
    vault, resource = uuid.uuid4(), uuid.uuid4()
    current = object()
    native = SimpleNamespace(get_current_resource=AsyncMock(return_value=current))
    if missing:
        native.get_current_resource.side_effect = NotFoundError("Native Resource", str(resource))
    monkeypatch.setattr(service, "_vault_id", AsyncMock(return_value=vault))
    monkeypatch.setattr(service, "_native", AsyncMock(return_value=native))
    response = AsyncMock(return_value="identity-response")
    monkeypatch.setattr(service, "_response", response)
    if missing:
        with pytest.raises(NotFoundError):
            await service.get_by_resource_id("v", resource)
        response.assert_not_awaited()
    else:
        assert await service.get_by_resource_id("v", resource) == "identity-response"
        response.assert_awaited_once_with(vault="v", vault_id=vault, current=current)
    native.get_current_resource.assert_awaited_once_with(
        namespace_id=vault, surface="document", resource_id=resource,
    )
