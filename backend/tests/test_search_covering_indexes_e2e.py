"""Real local pgvector regression with synthetic embeddings; never production.

Opt in with AKB_SEARCH_PERF_DSN=postgresql://postgres@127.0.0.1:15434/postgres.
Each test owns a disposable database, not the database named in the DSN.
"""

import logging
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from app.services.vector_store.pgvector import PgvectorStore


@pytest_asyncio.fixture
async def local_store():
    dsn = os.environ.get("AKB_SEARCH_PERF_DSN")
    if not dsn:
        pytest.skip("Set AKB_SEARCH_PERF_DSN to a disposable local PostgreSQL")
    parts = urlsplit(dsn)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("This synthetic test is restricted to loopback PostgreSQL")
    admin = await asyncpg.connect(dsn)
    name = f"search_test_{uuid.uuid4().hex}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    store = PgvectorStore(
        dsn=urlunsplit(parts._replace(path=f"/{name}")), schema="vector_index",
        dense_dim=32, sparse_shape="posting",
    )
    try:
        await store.ensure_collection()
        yield store
    finally:
        if store._own_pool:
            await store._own_pool.close()
        await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()


async def definitions(store):
    pool = await store._pool()
    return dict(await pool.fetch(
        "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='vector_index'"
    ))


async def test_fresh_covering_indexes_and_existing_indexes_preserved(local_store):
    store = local_store
    indexes = await definitions(store)
    assert "INCLUDE (weight)" in indexes["posting_pkey"]
    for name in ["idx_vi_chunks_source_id", "idx_vi_chunks_vault_id"]:
        assert "INCLUDE (chunk_id)" in indexes[name]
    pool = await store._pool()
    # Emulate an existing installation. Startup must not rebuild large indexes.
    await pool.execute("ALTER TABLE vector_index.posting DROP CONSTRAINT posting_pkey")
    await pool.execute("ALTER TABLE vector_index.posting ADD PRIMARY KEY(term_id,chunk_id)")
    await pool.execute("DROP INDEX vector_index.idx_vi_chunks_vault_id")
    await pool.execute("CREATE INDEX idx_vi_chunks_vault_id ON vector_index.chunks(vault_id)")
    before = await definitions(store)
    store._ensured_collection = False
    await store.ensure_collection()
    assert await definitions(store) == before


async def test_synthetic_hybrid_acl_empty_scope_and_timing(local_store, caplog):
    store = local_store
    vaults = [str(uuid.uuid4()), str(uuid.uuid4())]
    sources = []
    for i in range(40):
        source = str(uuid.uuid4())
        sources.append(source)
        await store.upsert_one(
            chunk_id=str(uuid.uuid4()), source_id=source, vault_id=vaults[i % 2],
            source_type="document", content=f"PRIVATE synthetic {i}", section_path=None,
            chunk_index=0, dense=[1.0, i / 40, *([0.0] * 30)],
            sparse_indices=[1, i + 2], sparse_values=[1.0 + i / 64, 0.5],
        )
    args = dict(query_text="PRIVATE query", query_dense=[1.0, *([0.0] * 31)],
                query_sparse_indices=[1], query_sparse_values=[1.0], limit=10,
                prefetch_per_leg=50)
    with caplog.at_level(logging.INFO):
        hits = await store.hybrid_search(**args, source_ids=None, vault_ids=[vaults[0]])
    assert len(hits) == 10
    assert all(str(hit.source_id) in sources[::2] for hit in hits)
    assert "hybrid_timing" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert all(value not in caplog.text for value in vaults + sources)
    one = await store.hybrid_search(**args, source_ids=[sources[3]])
    assert [str(hit.source_id) for hit in one] == [sources[3]]
    assert await store.hybrid_search(**args, source_ids=[]) == []
    assert await store.hybrid_search(**args, source_ids=None, vault_ids=[]) == []
    sparse = await store.hybrid_search(**(args | {"query_dense": None}), source_ids=[sources[3]])
    dense = await store.hybrid_search(**(args | {"query_sparse_indices": [], "query_sparse_values": []}), source_ids=[sources[3]])
    assert [str(hit.source_id) for hit in sparse] == [sources[3]]
    assert [str(hit.source_id) for hit in dense] == [sources[3]]


async def test_startup_search_prewarm_is_sized_reported_and_shared(local_store):
    store = local_store
    await store.upsert_one(
        chunk_id=str(uuid.uuid4()),
        source_id=str(uuid.uuid4()),
        vault_id=str(uuid.uuid4()),
        source_type="document",
        content="small prewarm contract fixture",
        section_path=None,
        chunk_index=0,
        dense=[1.0, *([0.0] * 31)],
        sparse_indices=[1],
        sparse_values=[1.0],
    )
    store._startup_prewarm = "search"

    await store.startup_prewarm()

    status = store.startup_prewarm_status()
    assert status["state"] == "ready"
    assert status["source"] == "local"
    assert status["relation_bytes"] > 0
    assert status["loaded_blocks"] > 0

    peer = PgvectorStore(
        dsn=store._dsn,
        schema="vector_index",
        dense_dim=32,
        sparse_shape="posting",
        startup_prewarm="search",
    )
    try:
        await peer.ensure_collection()
        await peer.startup_prewarm()
        assert peer.startup_prewarm_status()["source"] == "peer"
    finally:
        if peer._own_pool:
            await peer._own_pool.close()
