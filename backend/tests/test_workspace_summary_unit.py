"""Workspace counts preserve scope, absence, snapshot boundaries, and auth."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api.deps import get_current_user
from app.api.routes import access
from app.services import document_counters, workspace_summary


class _Connection:
    def __init__(self):
        self.active = False
        self.queries = []
        self.transaction_options = None
        self.row = {
            "observed_at": datetime(2026, 9, 14, tzinfo=timezone.utc),
            "document_count": 10,
            "table_count": 2,
            "file_count": 3,
        }

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self, **options):
        self.transaction_options = options
        self.active = True
        try:
            yield self
        finally:
            self.active = False

    async def fetchrow(self, sql, *args):
        assert self.active
        self.queries.append((sql, args))
        return self.row


@pytest.fixture
def snapshot(monkeypatch):
    conn = _Connection()
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    async def directory(user_id, *, conn):
        assert user_id == "account-id"
        assert conn.active
        return [{"id": value} for value in [*ids, ids[0]]]

    monkeypatch.setattr(workspace_summary, "get_pool", AsyncMock(return_value=conn))
    monkeypatch.setattr(workspace_summary, "list_accessible_vaults", directory)
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: False)
    return conn, ids


async def test_summary_uses_one_readonly_snapshot_and_bounded_count_query(snapshot):
    conn, ids = snapshot
    result = await workspace_summary.get_workspace_summary("account-id")
    assert result == {
        "version": 1, "scope": "accessible", "observed_at": "2026-09-14T00:00:00+00:00",
        "vault_count": 2, "document_count": 10, "table_count": 2, "file_count": 3,
    }
    assert conn.transaction_options == {"isolation": "repeatable_read", "readonly": True}
    assert len(conn.queries) == 1
    query, args = conn.queries[0]
    assert args == ([uuid.UUID(value) for value in ids],)
    assert "FROM documents WHERE vault_id = ANY($1::uuid[])" in query
    assert "vf.kind = 'file' AND vf.upload_state = 'confirmed'" in query
    assert "information_schema" not in query
    assert "SELECT *" not in query


@pytest.mark.parametrize("value", [None, -1, True, "3", 1.5, 2**53])
async def test_invalid_or_unsafe_counts_are_absent_not_zero(snapshot, value):
    conn, _ = snapshot
    conn.row["document_count"] = value
    result = await workspace_summary.get_workspace_summary("account-id")
    assert "document_count" not in result
    assert result["table_count"] == 2


async def test_actual_zero_remains_zero(snapshot):
    conn, _ = snapshot
    conn.row.update(document_count=0, table_count=0, file_count=0)
    result = await workspace_summary.get_workspace_summary("account-id")
    assert [result[key] for key in ("document_count", "table_count", "file_count")] == [0, 0, 0]


async def test_failed_count_query_does_not_publish_partial_or_zero_summary(snapshot, monkeypatch):
    conn, _ = snapshot
    monkeypatch.setattr(conn, "fetchrow", AsyncMock(side_effect=RuntimeError("unavailable")))
    with pytest.raises(RuntimeError, match="unavailable"):
        await workspace_summary.get_workspace_summary("account-id")
    assert not conn.active


@pytest.mark.parametrize("native", [True, False])
def test_set_counter_uses_same_authority_as_existing_vault_count(monkeypatch, native):
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: native)
    single = document_counters.vault_document_count_sql()
    multiple = document_counters.scoped_document_count_sql()
    assert multiple == single.replace("= $1", "= ANY($1::uuid[])")


async def test_route_requires_auth_before_service(monkeypatch):
    service = AsyncMock()
    monkeypatch.setattr(access, "get_workspace_summary", service)
    app = FastAPI()
    app.include_router(access.router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/my/workspace-summary")
    assert response.status_code == 401
    service.assert_not_awaited()


async def test_route_is_account_scoped_not_request_scoped_and_not_cacheable(monkeypatch):
    service = AsyncMock(return_value={
        "version": 1, "scope": "accessible", "observed_at": "2026-09-14T00:00:00Z",
        "vault_count": 1, "document_count": 2, "table_count": None,
    })
    monkeypatch.setattr(access, "get_workspace_summary", service)
    app = FastAPI()
    app.include_router(access.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="signed-in-account")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/my/workspace-summary?user_id=someone-else&vault=private")
    assert response.status_code == 200
    service.assert_awaited_once_with("signed-in-account")
    assert response.headers["cache-control"] == "private, no-store"
    assert "table_count" not in response.json()
    assert "file_count" not in response.json()
