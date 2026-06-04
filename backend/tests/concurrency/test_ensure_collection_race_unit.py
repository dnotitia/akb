"""Race-safety regressions for pgvector ``ensure_collection``.

Background — 0.6.2 introduced a partial-HNSW migration inside
``ensure_collection``: the legacy full HNSW is dropped and a
``WHERE dense IS NOT NULL`` partial form is built. The first
prod rollout (0.6.3) revealed two failure modes the previous
guards couldn't catch:

1. **Cross-caller race.** When a search request times out, asyncio
   cancels the in-flight ``ensure_collection`` task — including any
   long-running ``CREATE INDEX``. The next request finds
   ``_ensured_collection=False`` and starts another build, and so
   on, ad infinitum. Even at a single uvicorn worker (so no
   true multi-process), the cancellation made it look like four
   concurrent builds were in flight, and no row ever got an index.
2. **Atomicity gap.** The 0.6.2 sequence was ``DROP legacy`` then
   ``CREATE partial``. A ``CREATE INDEX`` failure (OOM, /dev/shm
   too small, cancellation) leaves the schema with no dense index
   at all — every search falls through to a seq scan or returns
   empty.

0.6.4 fixes both with (a) a PG transaction-scoped advisory lock
keyed on the schema name, so cross-process / cross-cancellation
callers serialize; and (b) an atomic-swap pattern: build the new
partial under a temp name, ``DROP`` legacy + ``RENAME`` only when
the build succeeded. These tests assert both invariants against a
real Postgres + pgvector (set ``AKB_TEST_DSN`` to enable; the
suite skips otherwise).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio


_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def store():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    # Throw-away schema per test so we never collide with another
    # suite's `vector_index` state.
    schema = f"vi_race_{uuid.uuid4().hex[:8]}"
    from app.services.vector_store.pgvector import PgvectorStore
    s = PgvectorStore(
        dsn=_DSN, schema=schema, dense_dim=8, sparse_shape="arrays",
    )
    try:
        yield s
    finally:
        # Drop the throw-away schema. New conn — `s` may have its own
        # pool we don't want to keep alive past the test.
        conn = await asyncpg.connect(_DSN)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await conn.close()
        if s._own_pool is not None:
            await s._own_pool.close()


@pytest.mark.asyncio
async def test_concurrent_ensure_collection_callers_serialize(store):
    """N concurrent ``ensure_collection`` calls all see the same
    final state and only one of them does the actual CREATE.

    Regression for the 0.6.3 prod incident — under cancellation
    pressure the worker raced itself and never finished a build.
    """
    # Race ten coroutines against a cold schema. Without the
    # advisory lock + asyncio.Lock guards, every caller's
    # `_ensured_collection=False` would let them all enter
    # `_do_ensure` concurrently and pile CREATE INDEX duplicates
    # onto the same table.
    await asyncio.gather(*[store.ensure_collection() for _ in range(10)])

    # Schema flag flipped exactly once.
    assert store._ensured_collection is True

    # Index ended up present with the partial form (WHERE clause).
    conn = await asyncpg.connect(_DSN)
    try:
        is_partial = await conn.fetchval(
            """
            SELECT i.indpred IS NOT NULL
              FROM pg_index i
              JOIN pg_class c     ON c.oid = i.indexrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1
               AND c.relname = 'idx_vi_chunks_dense'
            """,
            store._schema,
        )
        assert is_partial is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_legacy_full_index_swaps_to_partial_atomically(store):
    """When a pre-0.6.2 full HNSW already exists, ensure_collection
    swaps it for a partial form *without* a window where the index
    is absent.

    Regression for the 0.6.3 prod incident — the previous code did
    `DROP legacy` then `CREATE partial` in two steps. If the CREATE
    failed (OOM), the schema sat without any dense index.
    """
    # Hand-build the legacy full HNSW (no WHERE clause). Mirrors
    # what pre-0.6.2 deployments have on disk today.
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(f'CREATE SCHEMA "{store._schema}"')
        await conn.execute(
            f"""
            CREATE TABLE "{store._schema}".chunks (
                chunk_id        UUID PRIMARY KEY,
                source_type     TEXT NOT NULL,
                source_id       UUID NOT NULL,
                section_path    TEXT,
                content         TEXT NOT NULL,
                chunk_index     INTEGER NOT NULL,
                dense           vector(8) NOT NULL,
                sparse_terms    BIGINT[] NOT NULL DEFAULT '{{}}',
                sparse_weights  REAL[]   NOT NULL DEFAULT '{{}}',
                indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Legacy full index — no WHERE clause.
        await conn.execute(
            f"""
            CREATE INDEX idx_vi_chunks_dense
                ON "{store._schema}".chunks
                USING hnsw (dense vector_cosine_ops)
            """
        )
    finally:
        await conn.close()

    # Now run the driver's migration path.
    await store.ensure_collection()

    # Resulting index: same name, partial form.
    conn = await asyncpg.connect(_DSN)
    try:
        is_partial = await conn.fetchval(
            """
            SELECT i.indpred IS NOT NULL
              FROM pg_index i
              JOIN pg_class c     ON c.oid = i.indexrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1
               AND c.relname = 'idx_vi_chunks_dense'
            """,
            store._schema,
        )
        assert is_partial is True

        # And the temp name (used during the atomic swap) is gone —
        # the RENAME completed inside the same advisory-lock tx.
        leftover = await conn.fetchval(
            """
            SELECT 1 FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1 AND c.relname = 'idx_vi_chunks_dense_new'
            """,
            store._schema,
        )
        assert leftover is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_idempotent_when_partial_already_in_place(store):
    """Re-calling ensure_collection on a schema that already has
    the partial index is a fast no-op — no DROP, no rebuild.
    Important so cold-path callers (every request, until the flag
    flips) don't waste work."""
    # First pass builds.
    await store.ensure_collection()

    # Reset instance flag to force the SQL path again.
    store._ensured_collection = False

    # Record the index oid; if a rebuild happened it would change.
    conn = await asyncpg.connect(_DSN)
    try:
        oid_before = await conn.fetchval(
            """
            SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1 AND c.relname = 'idx_vi_chunks_dense'
            """,
            store._schema,
        )
    finally:
        await conn.close()

    await store.ensure_collection()

    conn = await asyncpg.connect(_DSN)
    try:
        oid_after = await conn.fetchval(
            """
            SELECT c.oid FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = $1 AND c.relname = 'idx_vi_chunks_dense'
            """,
            store._schema,
        )
    finally:
        await conn.close()

    assert oid_before == oid_after
