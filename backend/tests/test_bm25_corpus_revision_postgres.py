"""Live PostgreSQL backstop for BM25 corpus invalidation tracking."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.db.postgres import _load_migration
from tests.helpers.pg_database import TestDatabase, database_template, temporary_database

pytestmark = pytest.mark.asyncio
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)

_INIT_SQL = Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"


async def _revision(conn) -> int:
    row = await conn.fetchrow(
        "SELECT last_value, is_called FROM bm25_corpus_revision_seq"
    )
    return int(row["last_value"]) if row["is_called"] else 0


async def test_migration_skips_optional_bm25_tables_when_not_materialized():
    class HistoricalBootstrapConnection:
        async def fetchval(self, query, *args):
            assert "to_regclass('public.bm25_stats')" in query
            return False

        def transaction(self):
            raise AssertionError("a skipped migration must not start DDL")

    migration = _load_migration("084_bm25_corpus_revision.py")
    assert migration is not None
    await migration.migrate(HistoricalBootstrapConnection())


async def _initialize_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(_INIT_SQL.read_text())
    for filename in (
        "005_qdrant_index.py",  # Materialize the optional BM25 vocabulary and stats.
        "084_bm25_corpus_revision.py",  # Track corpus changes.
        "111_bm25_recompute_resume.py",  # Keep the recompute cursor and partial counts.
    ):
        migration = _load_migration(filename)
        assert migration is not None
        await migration.migrate(conn)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def schema_template() -> AsyncIterator[TestDatabase]:
    async with database_template(_DSN, "akb_bm25_revision_template_", _initialize_schema) as template:
        yield template


@pytest_asyncio.fixture
async def connection(schema_template: TestDatabase) -> AsyncIterator[tuple[asyncpg.Connection, uuid.UUID]]:
    async with temporary_database(_DSN, "akb_bm25_revision_", template=schema_template) as database:
        conn = await asyncpg.connect(database.dsn)
        try:
            vault_name = f"bm25-revision-{uuid.uuid4().hex[:10]}"
            vault_id = await conn.fetchval(
                "INSERT INTO vaults(name, git_path) VALUES($1, $2) RETURNING id",
                vault_name,
                f"/tmp/{vault_name}.git",
            )
            yield conn, vault_id
        finally:
            await conn.close()


@pytest.mark.live_services
async def test_only_bm25_relevant_chunk_mutations_advance_revision(connection):
    conn, vault_id = connection
    chunk_id = uuid.uuid4()
    source_id = uuid.uuid4()
    before = await _revision(conn)

    await conn.execute(
        """
        INSERT INTO chunks(
            id, source_type, source_id, vault_id,
            section_path, content, chunk_index
        ) VALUES($1, 'document', $2, $3, '', 'alpha', 0)
        """,
        chunk_id,
        source_id,
        vault_id,
    )
    assert await _revision(conn) == before + 1

    # Worker claim/index bookkeeping cannot invalidate corpus statistics.
    await conn.execute(
        "UPDATE chunks SET vector_retry_count = vector_retry_count + 1 WHERE id = $1",
        chunk_id,
    )
    assert await _revision(conn) == before + 1

    await conn.execute(
        "UPDATE chunks SET content = 'beta' WHERE id = $1",
        chunk_id,
    )
    assert await _revision(conn) == before + 2

    await conn.execute("DELETE FROM chunks WHERE id = $1", chunk_id)
    assert await _revision(conn) == before + 3


@pytest.mark.live_services
async def test_recompute_uses_source_revision_not_token_bearing_doc_count(
    connection,
    monkeypatch,
):
    from app.services import sparse_encoder

    conn, vault_id = connection
    source_id = uuid.uuid4()
    for index, content in enumerate(("alpha", "베타", "")):
        await conn.execute(
            """
            INSERT INTO chunks(
                id, source_type, source_id, vault_id,
                section_path, content, chunk_index
            ) VALUES($1, 'document', $2, $3, '', $4, $5)
            """,
            uuid.uuid4(),
            source_id,
            vault_id,
            content,
            index,
        )

    class SingleConnectionPool:
        def acquire(self):
            @asynccontextmanager
            async def acquired():
                yield conn

            return acquired()

    async def get_single_pool():
        return SingleConnectionPool()

    monkeypatch.setattr(sparse_encoder, "get_pool", get_single_pool)
    result = await sparse_encoder.recompute_stats(batch_size=2)

    assert result["source_chunk_count"] == 3
    assert result["total_docs"] == 2
    assert not await sparse_encoder._should_recompute()
