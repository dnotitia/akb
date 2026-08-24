"""Live PostgreSQL backstop for BM25 corpus invalidation tracking."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


async def _revision(conn) -> int:
    row = await conn.fetchrow(
        "SELECT last_value, is_called FROM bm25_corpus_revision_seq"
    )
    return int(row["last_value"]) if row["is_called"] else 0


async def test_migration_skips_optional_bm25_tables_when_not_materialized():
    from app.db.postgres import _load_migration

    class HistoricalBootstrapConnection:
        async def fetchval(self, query, *args):
            assert "to_regclass('public.bm25_stats')" in query
            return False

        def transaction(self):
            raise AssertionError("a skipped migration must not start DDL")

    migration = _load_migration("084_bm25_corpus_revision.py")
    assert migration is not None
    await migration.migrate(HistoricalBootstrapConnection())


@pytest.fixture
async def connection():
    try:
        conn = await asyncpg.connect(_DSN, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    from app.db.postgres import _load_migration

    # ``init.sql`` intentionally leaves historical derived-index tables to
    # migration 005.  Make this focused test self-contained on a fresh schema.
    if not await conn.fetchval("SELECT to_regclass('public.bm25_stats')"):
        bm25_base = _load_migration("005_qdrant_index.py")
        assert bm25_base is not None
        await bm25_base.migrate(conn)
    migration = _load_migration("084_bm25_corpus_revision.py")
    assert migration is not None
    await migration.migrate(conn)

    vault_name = f"bm25-revision-{uuid.uuid4().hex[:10]}"
    vault_id = await conn.fetchval(
        "INSERT INTO vaults(name, git_path) VALUES($1, $2) RETURNING id",
        vault_name,
        f"/tmp/{vault_name}.git",
    )
    try:
        yield conn, vault_id
    finally:
        await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)
        await conn.close()


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
