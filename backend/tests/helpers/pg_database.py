"""Create disposable PostgreSQL databases for tests that need isolated state."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest


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
