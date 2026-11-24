"""Create disposable PostgreSQL databases for tests that need isolated state."""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio


@dataclass(frozen=True, slots=True)
class TestDatabase:
    __test__ = False

    name: str
    dsn: str


@asynccontextmanager
async def temporary_database(
    admin_dsn: str,
    prefix: str,
    *,
    template: TestDatabase | None = None,
    required: bool = False,
) -> AsyncIterator[TestDatabase]:
    """Create an empty database or clone a template with no live connections."""
    name = f"{prefix}{uuid4().hex[:12]}"
    database = TestDatabase(name, urlsplit(admin_dsn)._replace(path=f"/{name}").geturl())
    try:
        admin = await asyncpg.connect(admin_dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("REQUIRE_REAL_PG=1 but the test PostgreSQL server is unreachable")
        if required or template is not None:
            raise
        pytest.skip("Test PostgreSQL server is unreachable")

    try:
        template_sql = f' TEMPLATE "{template.name}"' if template is not None else ""
        await admin.execute(f'CREATE DATABASE "{name}"{template_sql}')
        try:
            yield database
        finally:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    finally:
        await admin.close()


@asynccontextmanager
async def database_template(
    admin_dsn: str,
    prefix: str,
    initialize: Callable[[asyncpg.Connection], Awaitable[None]],
    *,
    required: bool = False,
) -> AsyncIterator[TestDatabase]:
    """Keep an initialized database available for cloning, with no connections open."""
    async with temporary_database(admin_dsn, prefix, required=required) as template:
        conn = await asyncpg.connect(template.dsn)
        try:
            await initialize(conn)
        finally:
            await conn.close()
        yield template


_INIT_SQL_PATH = Path(__file__).resolve().parents[2] / "app" / "db" / "init.sql"
_DEFAULT_TEST_DSN = "postgresql://akb:akb@localhost:15432/akb"  # pragma: allowlist secret
AUDIT_TEST_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


async def _initialize_base_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(_INIT_SQL_PATH.read_text(encoding="utf-8"))


async def initialize_application_schema(conn: asyncpg.Connection) -> None:
    """Apply the application's base schema and all regular migrations."""
    from app.db import postgres

    await postgres._run_boot_schema(conn, init_sql=_INIT_SQL_PATH.read_text(encoding="utf-8"))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def base_schema_template():
    """Share the schema from init.sql without running migrations."""
    dsn = os.environ.get("AKB_TEST_DSN", _DEFAULT_TEST_DSN)
    async with database_template(dsn, "akb_base_schema_template_", _initialize_base_schema) as template:
        yield template


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def application_schema_template():
    """Share one regular schema with integration tests on the main test server."""
    dsn = os.environ.get("AKB_TEST_DSN", _DEFAULT_TEST_DSN)
    async with database_template(dsn, "akb_application_template_", initialize_application_schema) as template:
        yield template


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _local_audit_application_schema_template():
    async with database_template(
        AUDIT_TEST_DSN, "akb_audit_application_template_", initialize_application_schema,
    ) as template:
        yield template


@pytest.fixture(scope="session")
def audit_application_schema_template(request: pytest.FixtureRequest) -> TestDatabase:
    """Use the regular template when both suites target the same server."""
    if "AKB_TEST_DSN" in os.environ:
        return request.getfixturevalue("application_schema_template")
    return request.getfixturevalue("_local_audit_application_schema_template")
