"""Shared PostgreSQL schema snapshots for account and SSO tests."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import os
from pathlib import Path

import asyncpg
import pytest_asyncio

from tests.helpers.pg_database import TestDatabase, database_template, temporary_database

_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


async def apply_auth_migrations(conn: asyncpg.Connection, *filenames: str) -> None:
    from app.db.postgres import _load_migration

    for filename in filenames:
        migration = _load_migration(filename)
        assert migration is not None
        await migration.migrate(conn=conn)


async def _initialize_auth_schema(conn: asyncpg.Connection) -> None:
    init_sql = Path(__file__).resolve().parents[2] / "app" / "db" / "init.sql"
    await conn.execute(init_sql.read_text(encoding="utf-8"))
    await apply_auth_migrations(conn, "015_events_outbox.py")


@asynccontextmanager
async def auth_database_template(
    source: TestDatabase,
    prefix: str,
    initialize: Callable[[asyncpg.Connection], Awaitable[None]],
) -> AsyncIterator[TestDatabase]:
    async with temporary_database(_DSN, prefix, template=source) as template:
        conn = await asyncpg.connect(template.dsn)
        try:
            await initialize(conn)
        finally:
            await conn.close()
        yield template


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def auth_schema_template():
    async with database_template(_DSN, "akb_auth_template_", _initialize_auth_schema) as template:
        yield template


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def admin_auth_template(auth_schema_template):
    async def initialize(conn):
        await apply_auth_migrations(conn, "076_sso_session_epoch.py")

    async with auth_database_template(auth_schema_template, "akb_admin_auth_template_", initialize) as template:
        yield template


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser_auth_template(admin_auth_template):
    async def initialize(conn):
        await apply_auth_migrations(conn, "137_mcp_monitoring.py", "138_monitoring_access.py")

    async with auth_database_template(admin_auth_template, "akb_sso_browser_template_", initialize) as template:
        yield template


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def identity_operations_template(admin_auth_template):
    async def initialize(conn):
        await apply_auth_migrations(conn, "082_pending_admissions.py")
        installation = await conn.fetchval("SELECT installation_id FROM sso_operation_installation")
        await apply_auth_migrations(conn, "121_sso_identity_operations.py", "121_sso_identity_operations.py")
        assert await conn.fetchval("SELECT installation_id FROM sso_operation_installation") == installation

    async with auth_database_template(admin_auth_template, "akb_sso_ops_template_", initialize) as template:
        yield template


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def operation_requests_template(identity_operations_template):
    async def initialize(conn):
        await apply_auth_migrations(
            conn,
            "127_sso_operation_requests.py",
            "127_sso_operation_requests.py",
            "129_sso_request_recovery.py",
            "129_sso_request_recovery.py",
        )

    async with auth_database_template(identity_operations_template, "akb_sso_requests_template_", initialize) as template:
        yield template
