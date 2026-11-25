"""Shared PostgreSQL schemas for native revision and graph integration tests."""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest_asyncio

from tests.helpers.pg_database import TestDatabase, database_template, temporary_database


_BACKEND = Path(__file__).resolve().parents[2]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "048_native_revision_core.py"
_INDEXABLE_MIGRATIONS = (
    _BACKEND / "app" / "db" / "migrations" / "005_qdrant_index.py",
    _BACKEND / "app" / "db" / "migrations" / "006_indexable_chunks.py",
)
_BODY_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "053_native_revision_m1_pg_body.py"
_PLACEMENT_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "057_native_revision_m1_payload_placement.py"
_DERIVED_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "054_native_revision_searchable_derived.py"
_FILE_DERIVED_MIGRATION = (
    _BACKEND / "app" / "db" / "migrations" / "059_native_file_searchable_derived.py"
)
_WRITE_POLICY_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "044_vault_write_policy.py"
# The document write path publishes image references; 107 is what gives a
# Native document a column to publish into.
_ASSET_REF_MIGRATION = (
    _BACKEND / "app" / "db" / "migrations" / "107_native_document_asset_refs.py"
)
NATIVE_TEST_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)
# Migration 057 only runs on measurement databases. This prefix lets it apply
# the same placement-scoped payload constraints as the Native measurement arm.
_LEDGER_DATABASE_PREFIX = "akb_revision_m1_measurement_bcore_"


def _load_migration(path: Path = _MIGRATION):
    spec = importlib.util.spec_from_file_location("native_revision_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _initialize_ledger_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(_INIT_SQL)
    await _load_migration(_MIGRATION.with_name("015_events_outbox.py")).migrate(conn=conn)
    await _load_migration(_MIGRATION.with_name("019_s3_delete_outbox.py")).migrate(conn=conn)
    migration = _load_migration()
    await migration.migrate(conn=conn)
    await migration.migrate(conn=conn)  # idempotent startup/retry
    await _load_migration(_BODY_MIGRATION).migrate(conn=conn)
    await _load_migration(_PLACEMENT_MIGRATION).migrate(conn=conn)
    await _load_migration(_ASSET_REF_MIGRATION).migrate(conn=conn)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def native_template() -> AsyncIterator[TestDatabase]:
    async with database_template(NATIVE_TEST_DSN, _LEDGER_DATABASE_PREFIX, _initialize_ledger_schema) as template:
        yield template


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def native_derived_template(native_template: TestDatabase) -> AsyncIterator[TestDatabase]:
    async with temporary_database(NATIVE_TEST_DSN, _LEDGER_DATABASE_PREFIX, template=native_template) as database:
        conn = await asyncpg.connect(database.dsn)
        try:
            for path in _INDEXABLE_MIGRATIONS:
                await _load_migration(path).migrate(conn=conn)
            await _load_migration(_WRITE_POLICY_MIGRATION).migrate(conn=conn)
            await _load_migration(_DERIVED_MIGRATION).migrate(conn=conn)
            await _load_migration(_FILE_DERIVED_MIGRATION).migrate(conn=conn)
        finally:
            await conn.close()
        yield database


@asynccontextmanager
async def cloned_ledger_database(
    template: TestDatabase, *, pool_max_size: int = 8
) -> AsyncIterator[tuple[asyncpg.Pool, uuid.UUID]]:
    async with temporary_database(NATIVE_TEST_DSN, _LEDGER_DATABASE_PREFIX, template=template) as database:
        async with asyncpg.create_pool(database.dsn, min_size=1, max_size=pool_max_size) as pool:
            async with pool.acquire() as seeded:
                vault_id = await seeded.fetchval(
                    "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
                    f"native-{uuid.uuid4().hex}",
                    "/tmp/legacy-unused.git",
                )
            yield pool, vault_id


# Migration 057 requires the measurement database prefix to permit mixed placements.
DERIVED_DATABASE_PREFIX = "akb_revision_m1_measurement_derived_"


async def _initialize_derived_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(_INIT_SQL)
    # 112 is here because the derived pipeline now writes `edges`, and that
    # table's resource-identity foreign keys and partial indexes come from
    # the migration, not from init.sql (init.sql re-runs against existing
    # databases, where an index on a column `CREATE TABLE IF NOT EXISTS`
    # never added would fail the boot schema). Without it this fixture
    # builds an `edges` no deployment has. 113 is the vocabulary epoch the
    # embed worker's write reads under the term-id fence (akb#687).
    for number in (5, 6, 15, 48, 53, 54, 55, 56, 57, 59, 89, 107, 112, 113):
        path = next((_BACKEND / "app" / "db" / "migrations").glob(f"{number:03d}_*.py"))
        migration = _load_migration(path)
        await migration.migrate(conn=conn)
        if number in (54, 59):
            await migration.migrate(conn=conn)  # startup retry is idempotent


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def derived_pipeline_template() -> AsyncIterator[TestDatabase]:
    async with database_template(NATIVE_TEST_DSN, DERIVED_DATABASE_PREFIX, _initialize_derived_schema) as template:
        yield template


@asynccontextmanager
async def cloned_derived_database(template: TestDatabase):
    async with temporary_database(NATIVE_TEST_DSN, DERIVED_DATABASE_PREFIX, template=template) as database:
        async with asyncpg.create_pool(database.dsn, min_size=1, max_size=8) as pool:
            yield pool
