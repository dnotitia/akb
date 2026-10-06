"""Search SQL routing against exhaustive oracles in a dedicated test database.

Point AKB_SEARCH_TEST_DSN (or AKB_VCHORD_TEST_DSN) at disposable PostgreSQL
with pgvector >=0.8. A fresh database is created and dropped for each case.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from pgvector.asyncpg import register_vector

from app.services.vector_store import pgvector as pgvector_module
from app.services.vector_store.pgvector import PgvectorStore

pytestmark = pytest.mark.asyncio
_VAULT = uuid.UUID(int=100)
_OTHER_VAULT = uuid.UUID(int=101)
_SOURCE = uuid.UUID(int=200)
_OTHER_SOURCE = uuid.UUID(int=201)


@pytest.fixture
async def store_pool():
    dsn = os.environ.get("AKB_SEARCH_TEST_DSN") or os.environ.get("AKB_VCHORD_TEST_DSN")
    if not dsn:
        pytest.skip("AKB_SEARCH_TEST_DSN or AKB_VCHORD_TEST_DSN is required")
    name = f"akb_scope_routes_{uuid.uuid4().hex[:12]}"
    parsed = urlsplit(dsn)
    test_dsn = urlunsplit(parsed._replace(path=f"/{name}"))
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        bootstrap = await asyncpg.connect(test_dsn)
        try:
            await bootstrap.execute("CREATE EXTENSION vector")
        finally:
            await bootstrap.close()
        pool = await asyncpg.create_pool(test_dsn, min_size=2, max_size=4, init=register_vector)

        async def get_pool():
            return pool

        store = PgvectorStore(
            dsn=None, schema="vector_index", dense_dim=4, sparse_shape="posting", get_main_pool=get_pool,
        )
        await store.ensure_collection()
        yield store, pool
    finally:
        if pool is not None:
            await asyncio.wait_for(pool.close(), timeout=5)
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


class _ObservedConnection:
    """Keep real PostgreSQL I/O and expose returned scores / executed plans."""
    def __init__(self, conn):
        self.conn = conn
        self.scored_rows = []
        self.dense_query = None
        self.queries = []

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def fetch(self, sql, *args, **kwargs):
        rows = await self.conn.fetch(sql, *args, **kwargs)
        self.queries.append((sql, args))
        if "<=>" in sql:
            self.dense_query = (sql, args)
        if rows and "score" in rows[0]:
            self.scored_rows = rows
        return rows


def _plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


async def _seed_dense(conn):
    rng = random.Random(1729)
    records = []
    for i in range(1, 4001):
        records.append((
            uuid.UUID(int=i), "document" if i % 2 == 0 else "table",
            _SOURCE if i % 7 == 0 else _OTHER_SOURCE,
            _VAULT if i % 7 == 0 else _OTHER_VAULT,
            "", f"chunk {i}", i,
            [1.0, rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)],
        ))
    await conn.copy_records_to_table(
        "chunks", schema_name="vector_index", records=records,
        columns=["chunk_id", "source_type", "source_id", "vault_id", "section_path", "content", "chunk_index", "dense"],
    )
    await conn.execute("ANALYZE vector_index.chunks")
    # These tests deliberately exercise the approximate index path. Removing
    # alternative filter indexes in this private fixture avoids proving only
    # that a bitmap/heap scan returns correctly sorted exact results.
    indexes = await conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname='vector_index' AND tablename='chunks' "
        "AND indexname NOT IN ('chunks_pkey', 'idx_vi_chunks_dense')",
    )
    for row in indexes:
        await conn.execute(f'DROP INDEX vector_index."{row["indexname"]}"')


@pytest.mark.parametrize("scope", ["type_only", "vault", "source", "tied_distance"])
async def test_hnsw_candidates_are_distance_sorted_after_repeated_filtered_searches(store_pool, scope):
    store, pool = store_pool
    async with pool.acquire() as conn:
        await _seed_dense(conn)
        if scope == "tied_distance":
            # HNSW may emit equal-distance candidates in graph visitation order.
            # The caller still needs stable ranks among whatever candidates ANN chose.
            await conn.execute(
                "UPDATE vector_index.chunks SET dense='[1,0,0,0]'::vector, "
                "source_type='document', vault_id=$1 WHERE chunk_index<=160", _VAULT,
            )
        observed = _ObservedConnection(conn)
        filter_col = "source_id" if scope == "source" else "vault_id"
        allowed = None if scope == "type_only" else [_SOURCE if scope == "source" else _VAULT]
        # More than the old HNSW default ef_search=40 and more than asyncpg's
        # first five custom executions before PostgreSQL considers a generic plan.
        for iteration in range(8):
            query = [1.0, (iteration % 2) * 0.02, 0.0, 0.0]
            async with conn.transaction():
                await conn.execute("SET LOCAL enable_seqscan=off")
                await conn.execute("SET LOCAL enable_bitmapscan=off")
                hits = await store._search_dense(
                    observed, query_dense=query, filter_uuids=allowed, filter_col=filter_col,
                    source_type_values=["document"], limit=80,
                )
                assert len(hits) == len(set(hits)) == 80
                assert observed.dense_query is not None
                sql, params = observed.dense_query
                plan = json.loads(await conn.fetchval("EXPLAIN (FORMAT JSON) " + sql, *params))[0]["Plan"]
                assert any(node.get("Index Name") == "idx_vi_chunks_dense" for node in _plan_nodes(plan))

            # This oracle scans every authorized vector without an ANN index.
            # ANN chooses candidates; their ranks must match exhaustive distance
            # order. We do not assert perfect approximate-neighbor recall.
            async with conn.transaction():
                await conn.execute("SET LOCAL enable_indexscan=off")
                await conn.execute("SET LOCAL enable_bitmapscan=off")
                oracle = await conn.fetch(
                    f"SELECT chunk_id::text AS chunk_id, dense <=> $1 AS distance "
                    f"FROM vector_index.chunks WHERE source_type='document' AND dense IS NOT NULL "
                    f"{'AND ' + filter_col + ' = ANY($2::uuid[]) ' if allowed else ''}"
                    "ORDER BY (dense <=> $1) + 0, chunk_id",
                    query, *([allowed] if allowed else []),
                )
            expected = [row["chunk_id"] for row in oracle if row["chunk_id"] in set(hits)]
            assert hits == expected  # Also proves every returned ID passed both filters.


async def _seed_postings(conn, count=180):
    chunks, postings = [], []
    # Reverse physical insertion order makes UUID tie ordering observable.
    for i in range(count, 0, -1):
        cid = uuid.UUID(int=i)
        chunks.append((
            cid, "table" if i % 5 == 0 else "document",
            _SOURCE if i % 3 < 2 else _OTHER_SOURCE,
            _VAULT if i <= 120 else _OTHER_VAULT, "", f"chunk {i}", i,
        ))
        postings.append((99, cid, 1000.0))  # Irrelevant term must not contribute.
        if i % 17:
            postings.append((7, cid, float(i % 4 + 1)))
            if i % 3:
                postings.append((11, cid, float(i % 6 + 1)))
    await conn.copy_records_to_table(
        "chunks", schema_name="vector_index", records=chunks,
        columns=["chunk_id", "source_type", "source_id", "vault_id", "section_path", "content", "chunk_index"],
    )
    await conn.copy_records_to_table(
        "posting", schema_name="vector_index", records=postings, columns=["term_id", "chunk_id", "weight"],
    )
    await conn.execute("ANALYZE vector_index.chunks")
    await conn.execute("ANALYZE vector_index.posting")


async def _posting_oracle(conn, filter_col, allowed, limit):
    # Independent exhaustive query: CASE encodes hand-picked weights, with no
    # dispatch, candidate materialization, or point-probe SQL from the driver.
    return await conn.fetch(
        f"SELECT c.chunk_id::text AS chunk_id, "
        "SUM(CASE p.term_id WHEN 7 THEN p.weight * 0.5 WHEN 11 THEN p.weight * 2.0 END) AS score "
        "FROM vector_index.chunks c JOIN vector_index.posting p USING (chunk_id) "
        f"WHERE c.{filter_col}=ANY($1::uuid[]) AND c.source_type='document' AND p.term_id IN (7,11) "
        "GROUP BY c.chunk_id ORDER BY score DESC, c.chunk_id ASC LIMIT $2", allowed, limit,
    )


@pytest.mark.parametrize("strategy", ["point", "scope", "term"])
@pytest.mark.parametrize("filter_col,allowed", [("vault_id", [_VAULT]), ("source_id", [_SOURCE])])
@pytest.mark.parametrize("limit", [7, 500])
async def test_posting_routes_match_exhaustive_scores_order_and_acl(store_pool, monkeypatch, caplog,
                                                                   strategy, filter_col, allowed, limit):
    store, pool = store_pool

    async def force_strategy(*args, **kwargs):
        return strategy

    monkeypatch.setattr(pgvector_module, "estimate_scope_strategy", force_strategy)
    caplog.set_level(logging.DEBUG, logger="akb.vector_store.pgvector")
    async with pool.acquire() as conn:
        await _seed_postings(conn)
        expected = await _posting_oracle(conn, filter_col, allowed, limit)
        observed = _ObservedConnection(conn)
        for _ in range(8):
            actual = await store._search_sparse(
                observed, terms=[7, 11, 123456], weights=[0.5, 2.0, 3.0],
                filter_uuids=allowed, filter_col=filter_col, source_type_values=["document"], limit=limit,
            )
            assert actual == [row["chunk_id"] for row in expected]
            assert [row["chunk_id"] for row in observed.scored_rows] == actual
            assert [float(row["score"]) for row in observed.scored_rows] == pytest.approx(
                [float(row["score"]) for row in expected],
            )
        assert expected  # A missing result path cannot satisfy this oracle.
        routes = [record.getMessage() for record in caplog.records
                  if record.getMessage().startswith("posting_scope_strategy=")]
        assert routes and all(f"posting_scope_strategy={strategy} " in route for route in routes)


@pytest.mark.parametrize("filter_name", ["source_ids", "vault_ids"])
async def test_empty_acl_never_returns_unscoped_hybrid_results(store_pool, filter_name):
    store, pool = store_pool
    async with pool.acquire() as conn:
        await _seed_postings(conn)
    filters = {"source_ids": None, "vault_ids": None, filter_name: []}
    assert await store.hybrid_search(
        query_text="term", query_dense=[1.0, 0.0, 0.0, 0.0], query_sparse_indices=[7], query_sparse_values=[1.0],
        **filters, source_types=["document"], limit=10, prefetch_per_leg=50,
    ) == []


@pytest.mark.parametrize("count,term_count", [(10_050, 1), (2_000, 51)], ids=["chunk-cap", "probe-cap"])
async def test_stale_point_estimate_falls_back_without_capping_matches(store_pool, monkeypatch, caplog,
                                                                     count, term_count):
    store, pool = store_pool

    async def force_point(*args, **kwargs):
        return "point"

    monkeypatch.setattr(pgvector_module, "estimate_scope_strategy", force_point)
    caplog.set_level(logging.DEBUG, logger="akb.vector_store.pgvector")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vector_index.chunks "
            "(chunk_id, source_type, source_id, vault_id, section_path, content, chunk_index) "
            "SELECT lpad(to_hex(i),32,'0')::uuid, 'document', $1, $2, '', i::text, i "
            "FROM generate_series(1,$3::int) AS i", _SOURCE, _VAULT, count,
        )
        await conn.execute(
            "INSERT INTO vector_index.posting (term_id, chunk_id, weight) "
            "SELECT 7, chunk_id, chunk_index::real FROM vector_index.chunks",
        )
        expected = await _posting_oracle(conn, "vault_id", [_VAULT], 10)
        hits = await store._search_sparse(
            conn, terms=[7] + list(range(1000, 1000 + term_count - 1)), weights=[0.5] * term_count,
            filter_uuids=[_VAULT], filter_col="vault_id", source_type_values=["document"], limit=10,
        )
        assert hits == [row["chunk_id"] for row in expected]
        assert uuid.UUID(hits[0]).int == count  # Highest match lives beyond a truncated scope.
        assert any("posting_scope_strategy=term" in record.getMessage() for record in caplog.records)
