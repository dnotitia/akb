"""A dynamic-table write that cannot get its Vault's event lock fails at commit
with `write_busy`, and its row is rolled back.

A statement that writes a dynamic table queues its row change; the event is
inserted by a deferred trigger when the transaction commits (migration 132).
With the Vault's event lock held elsewhere past its deadline, that commit
fails with SQLSTATE 55P03 and the constraint name `akb_event_lock_timeout`.
The SQL executor reports it as `write_busy`: REST `akb_sql` and row inserts
answer 429 with `Retry-After`, and MCP `akb_sql` returns the `write_busy`
envelope. The statement itself had succeeded, so these tests also check that
its row did not stay.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from app.config import settings

# Importing the MCP server or the API builds a GitService at module load,
# which creates `git_storage_path` — redirect it first.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-event-lock-commit-")

from app import main  # noqa: E402
from app.api.deps import get_current_user  # noqa: E402
from app.api.routes import tables  # noqa: E402
from app.repositories import table_data_repo  # noqa: E402
from app.services import table_row_write, table_service  # noqa: E402
from app.services.auth_service import AuthenticatedUser  # noqa: E402
from app.services.user_sql_executor import UserSqlExecutor  # noqa: E402
from mcp_server import server as srv  # noqa: E402
from tests.test_events_commit_order_postgres import (  # noqa: E402
    _OpenTransaction,
    _emit,
    _fresh_database,
    _make_vault,
)

pytestmark = pytest.mark.asyncio

_VAULT = "event-lock-commit"
_INSERT = "INSERT INTO items (value) VALUES ('x')"


async def _vault_with_a_table(pool: asyncpg.Pool) -> tuple[uuid.UUID, uuid.UUID, str]:
    """A Vault with one dynamic table, and a live human administrator."""
    vault_id = await _make_vault(pool, _VAULT)
    admin_id = uuid.uuid4()
    pg_name = table_data_repo.pg_table_name(_VAULT, "items")
    columns = [{"name": "value", "type": "text"}]
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES ($1, 'commit-admin', 'commit-admin@example.com', '!fixture!', TRUE)",
            admin_id,
        )
        await table_data_repo.create_dynamic_table(
            conn, pg_name, columns, vault_name=_VAULT, vault_id=vault_id,
            resource_uri=f"akb://{_VAULT}/table/items",
        )
        await conn.execute(
            "INSERT INTO vault_tables (id, vault_id, name, description, columns, unique_keys, indexes) "
            "VALUES ($1, $2, 'items', '', $3::jsonb, '[]'::jsonb, '[]'::jsonb)",
            uuid.uuid4(), vault_id, json.dumps(columns),
        )
    return vault_id, admin_id, pg_name


async def test_a_write_that_cannot_get_the_event_lock_at_commit_is_write_busy(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, admin_id, pg_name = await _vault_with_a_table(pool)
        executor = UserSqlExecutor(pool)
        monkeypatch.setattr(table_service, "get_user_sql_executor", lambda: executor)
        monkeypatch.setattr(table_row_write, "get_user_sql_executor", lambda: executor)
        access = AsyncMock(return_value={"vault_id": vault_id, "role": "admin"})
        monkeypatch.setattr(tables, "check_vault_access", access)
        monkeypatch.setattr(srv, "check_vault_access", access)
        monkeypatch.setattr(srv, "_get_user", AsyncMock(return_value=srv._MCPUser(
            str(admin_id), "commit-admin", is_admin=True, auth_method="pat",
        )))
        monkeypatch.setattr(srv.tool_usage, "record", lambda *a, **k: None)
        monkeypatch.setattr(srv.audit_log, "record_tool", lambda *a, **k: None)

        app = FastAPI(exception_handlers=dict(main.app.exception_handlers))
        app.include_router(tables.router, prefix="/api/v1")
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            user_id=str(admin_id), username="commit-admin", email="commit-admin@example.com",
            display_name=None, is_admin=True, auth_method="jwt",
        )

        async with pool.acquire() as holder, _OpenTransaction(holder):
            await _emit(holder, vault_id, "holder")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                responses = {
                    "sql": await client.post(f"/api/v1/tables/{_VAULT}/sql", json={"sql": _INSERT}),
                    "rows": await client.post(f"/api/v1/tables/{_VAULT}/items/rows", json={"value": "x"}),
                }
            mcp = await srv.call_tool("akb_sql", {"vault": _VAULT, "sql": _INSERT})

        for name, response in responses.items():
            assert response.status_code == 429, (name, response.text)
            assert response.json()["code"] == "write_busy", name
            assert response.headers["Retry-After"] == "1", name
        body = json.loads(mcp.content[0].text)
        assert mcp.is_error is True
        assert body["code"] == "write_busy"
        assert body["details"] == {"retry_after_secs": 1}
        # Each statement succeeded and its transaction then rolled back.
        assert await pool.fetchval(f"SELECT count(*) FROM {pg_name}") == 0
