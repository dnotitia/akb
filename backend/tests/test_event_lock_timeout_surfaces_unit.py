"""SQL and row-write requests report the Vault event lock's deadline as
`write_busy`, and keep reporting their own lock errors as SQL errors.

A statement that writes a dynamic table inserts its row-change event when its
transaction commits (migration 132). When the Vault's event lock stays busy
past its deadline the transaction fails with SQLSTATE 55P03 and the constraint
name `akb_event_lock_timeout`; nothing was written, so REST answers 429 with
`Retry-After` and MCP returns the `write_busy` envelope. A lock error of the
caller's own statement (a 55P03 without that name, or a deadlock) stays the
`sql_error` it has always been. The errors are raised where the SQL executor's
own error handling receives them.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings

# Importing the MCP server or the API builds a GitService at module load,
# which creates `git_storage_path` — redirect it first.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-event-lock-surfaces-")

from app import main  # noqa: E402
from app.api.deps import get_current_user  # noqa: E402
from app.api.routes import tables  # noqa: E402
from app.services import table_row_write, table_service  # noqa: E402
from app.services.auth_service import AuthenticatedUser  # noqa: E402
from app.services.user_sql_executor import UserSqlExecutor  # noqa: E402
from mcp_server import server as srv  # noqa: E402


def _event_lock_timeout() -> asyncpg.PostgresError:
    return asyncpg.PostgresError.new({
        "C": "55P03",
        "M": "events of Vault private-vault have been locked by another transaction for 5 seconds",
        "n": "akb_event_lock_timeout",
    })


_OWN_LOCK_ERRORS = [
    pytest.param(lambda: asyncpg.LockNotAvailableError("could not obtain lock on row"), "55P03", id="55P03"),
    pytest.param(lambda: asyncpg.DeadlockDetectedError("deadlock detected"), "40P01", id="40P01"),
]


@pytest.fixture
def executor_failing_with(monkeypatch):
    """Make every SQL execution fail with the given error."""

    def install(error: BaseException) -> None:
        @asynccontextmanager
        async def failing():
            raise error
            yield  # pragma: no cover

        @asynccontextmanager
        async def metadata():
            yield object()

        executor = UserSqlExecutor(SimpleNamespace(acquire=failing))
        monkeypatch.setattr(table_service, "get_pool", AsyncMock(return_value=SimpleNamespace(acquire=metadata)))
        monkeypatch.setattr(table_service.table_data_repo, "build_table_name_map", AsyncMock(return_value={}))
        monkeypatch.setattr(table_service, "get_user_sql_executor", lambda: executor)
        monkeypatch.setattr(table_row_write, "get_user_sql_executor", lambda: executor)
        monkeypatch.setattr(table_row_write, "_load_table", AsyncMock(return_value=table_row_write._TableInfo(
            columns=[{"name": "value", "type": "text"}], unique_keys=[],
        )))

    return install


@pytest.fixture
def client(monkeypatch):
    app = FastAPI(exception_handlers=dict(main.app.exception_handlers))
    app.include_router(tables.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=str(uuid.uuid4()), username="tester", email="tester@example.invalid",
        display_name=None, is_admin=False, auth_method="pat",
    )
    monkeypatch.setattr(tables, "check_vault_access", AsyncMock(
        return_value={"vault_id": uuid.uuid4(), "role": "writer"},
    ))
    return TestClient(app, raise_server_exceptions=False)


def _sql(client):
    return client.post("/api/v1/tables/private-vault/sql", json={"sql": "INSERT INTO metrics (value) VALUES ('x')"})


def _insert_row(client):
    return client.post("/api/v1/tables/private-vault/metrics/rows", json={"value": "x"})


@pytest.mark.parametrize("request_", [_sql, _insert_row], ids=["sql", "rows"])
def test_rest_reports_the_event_lock_timeout_as_write_busy(client, executor_failing_with, request_):
    executor_failing_with(_event_lock_timeout())
    response = request_(client)
    assert response.status_code == 429
    assert response.json()["code"] == "write_busy"
    assert response.headers["Retry-After"] == "1"
    assert "private-vault" not in response.text


@pytest.mark.parametrize("request_", [_sql, _insert_row], ids=["sql", "rows"])
@pytest.mark.parametrize(("error", "sqlstate"), _OWN_LOCK_ERRORS)
def test_rest_keeps_a_statements_own_lock_error_a_sql_error(client, executor_failing_with, request_, error, sqlstate):
    executor_failing_with(error())
    response = request_(client)
    assert response.status_code == 400
    assert response.json()["code"] == "sql_error"
    assert "Retry-After" not in response.headers


@pytest.fixture
def mcp_sql(monkeypatch):
    monkeypatch.setattr(srv.tool_usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(srv.audit_log, "record_tool", lambda *a, **k: None)
    monkeypatch.setattr(srv, "check_vault_access", AsyncMock(return_value={"vault_id": uuid.uuid4(), "role": "writer"}))

    def call() -> tuple[bool, dict]:
        out = asyncio.run(srv.call_tool(
            "akb_sql", {"vault": "private-vault", "sql": "INSERT INTO metrics (value) VALUES ('x')"},
        ))
        return out.is_error, json.loads(out.content[0].text)

    return call


def test_mcp_reports_the_event_lock_timeout_as_write_busy(mcp_sql, executor_failing_with):
    executor_failing_with(_event_lock_timeout())
    is_error, body = mcp_sql()
    assert is_error is True
    assert body["code"] == "write_busy"
    assert body["details"] == {"retry_after_secs": 1}
    assert "private-vault" not in json.dumps(body)


@pytest.mark.parametrize(("error", "sqlstate"), _OWN_LOCK_ERRORS)
def test_mcp_keeps_a_statements_own_lock_error_a_sql_error(mcp_sql, executor_failing_with, error, sqlstate):
    executor_failing_with(error())
    is_error, body = mcp_sql()
    assert is_error is True
    assert body["code"] == "sql_error"
