"""Regression: PgvectorStore must bootstrap the `vector` extension on a
DB that has never had it created.

Issue #117: on a fresh self-hosted Postgres (the `pgvector/pgvector`
image ships the extension *available* but not *created* in the app DB),
`ensure_collection()` registered the pgvector binary codec
(`register_vector` -> asyncpg `set_type_codec('vector', ...)`) *before*
running `CREATE EXTENSION vector`. asyncpg can't build a codec for a
type that doesn't exist yet, so it raised

    ValueError: unknown type: public.vector

That ValueError is not an `asyncpg.PostgresError`, so it escaped
`ensure_collection`'s except clause and surfaced at every
`hybrid_search` as `vector hybrid_search failed (unknown type:
public.vector); returning empty` — i.e. semantic search silently
returned nothing on every fresh install.

These tests exercise the *real* PgvectorStore against a throwaway DB
with no `vector` extension, in both pool modes:

  - own pool   (separate `vector_store_dsn`): the codec is registered
                in the pool `init` callback, so the extension must
                exist before the pool is built.
  - shared pool (blank DSN, reuse main pool): the codec is registered
                inside `ensure_collection`, so the extension must be
                created first there.

Both must succeed and leave the extension installed; a dense
round-trip then proves the codec actually took.

DB comes from ``AKB_TEST_DSN`` (default
``postgresql://akb:akb@localhost:15432/akb`` to match the dev override).  # pragma: allowlist secret
The DB must have the pgvector extension *available* (e.g. the
``pgvector/pgvector`` image). The module is skipped when no such
Postgres is reachable, so a plain ``pytest`` run stays a no-op rather
than a failure. Named ``*_e2e`` so the CI unit job
(``pytest -k 'not _e2e'``) excludes it — like the other DB-backed e2e
suites, it runs locally / against a deployment.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from app.services.vector_store.pgvector import PgvectorStore

_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


def _swap_db(dsn: str, dbname: str) -> str:
    """Return ``dsn`` with its database name replaced by ``dbname``."""
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


async def _vector_available(dsn: str) -> bool:
    """True iff the server can install pgvector (so a missing-extension
    test is meaningful — otherwise CREATE EXTENSION would fail for a
    reason unrelated to the bug under test)."""
    conn = await asyncpg.connect(dsn, timeout=2.0)
    try:
        row = await conn.fetchval(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        )
        return row is not None
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def fresh_db_dsn():
    """Create a throwaway database with NO `vector` extension, yield its
    DSN, and drop it on teardown."""
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    if not await _vector_available(_DSN):
        pytest.skip("pgvector extension not available on this server")

    admin = await asyncpg.connect(_DSN)
    dbname = f"pgvec_ext_regr_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        # Sanity: the fresh DB must NOT have the extension yet, else the
        # test proves nothing.
        probe = await asyncpg.connect(_swap_db(_DSN, dbname))
        try:
            present = await probe.fetchval(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            )
        finally:
            await probe.close()
        assert present is None, "throwaway DB unexpectedly has the vector extension"

        yield _swap_db(_DSN, dbname)
    finally:
        # FORCE terminates any leftover pool connections (PG13+).
        await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        await admin.close()


async def _extension_installed(dsn: str) -> bool:
    conn = await asyncpg.connect(dsn)
    try:
        return (
            await conn.fetchval(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            )
        ) is not None
    finally:
        await conn.close()


async def _dense_roundtrip(pool) -> list[float]:
    """Push a list[float] through the registered binary codec and read
    it back. Fails loudly if the codec isn't registered."""
    async with pool.acquire() as c:
        val = await c.fetchval("SELECT $1::vector(4)", [0.25, 0.5, 0.75, 1.0])
    return [float(x) for x in val]


async def test_ensure_collection_bootstraps_extension_own_pool(fresh_db_dsn):
    """Own-pool mode (separate vector_store_dsn): codec is registered in
    the pool init callback, so the extension must be created before the
    pool is built."""
    store = PgvectorStore(
        dsn=fresh_db_dsn,
        schema="vector_index",
        dense_dim=4,
        sparse_shape="posting",
        get_main_pool=None,
    )

    # The regression: this raised `ValueError: unknown type: public.vector`
    # before the fix.
    await store.ensure_collection()

    assert await _extension_installed(fresh_db_dsn), (
        "ensure_collection did not install the vector extension"
    )

    pool = await store._pool()
    assert await _dense_roundtrip(pool) == pytest.approx([0.25, 0.5, 0.75, 1.0])

    # Full round-trip through the public surface: the path that returned
    # empty for the user must now return the chunk.
    chunk_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    await store.upsert_one(
        chunk_id=chunk_id,
        content="symantec dlp cpu spike",
        section_path=None,
        chunk_index=0,
        dense=[0.25, 0.5, 0.75, 1.0],
        sparse_indices=[1, 2],
        sparse_values=[1.0, 0.5],
        source_type="document",
        source_id=source_id,
    )
    hits = await store.hybrid_search(
        query_text="cpu spike",
        query_dense=[0.25, 0.5, 0.75, 1.0],
        query_sparse_indices=[1, 2],
        query_sparse_values=[1.0, 0.5],
        source_ids=None,
        limit=5,
        prefetch_per_leg=10,
    )
    assert len(hits) >= 1, "hybrid_search returned empty after the fix"

    if store._own_pool is not None:
        await store._own_pool.close()


async def test_ensure_collection_bootstraps_extension_shared_pool(fresh_db_dsn):
    """Shared-pool mode (blank DSN): the driver reuses a main pool whose
    connections have NO register_vector init callback, so the codec is
    registered inside ensure_collection — the extension must be created
    first there."""
    main_pool = await asyncpg.create_pool(dsn=fresh_db_dsn, min_size=1, max_size=4)
    try:
        store = PgvectorStore(
            dsn=None,
            schema="vector_index",
            dense_dim=4,
            sparse_shape="posting",
            get_main_pool=lambda: _ready(main_pool),
        )
        await store.ensure_collection()
        assert await _extension_installed(fresh_db_dsn)
        assert await _dense_roundtrip(main_pool) == pytest.approx(
            [0.25, 0.5, 0.75, 1.0]
        )
    finally:
        await main_pool.close()


async def _ready(pool):
    """get_main_pool is awaited by the driver; wrap the already-created
    pool in a coroutine."""
    return pool
