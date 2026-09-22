"""Real Kiwi + VectorChord evidence; only disposable AKB_VCHORD_TEST_DSN databases.

The exact oracle materializes the authorized rows before scoring, so neither
its membership nor its order depends on the extension's candidate budget.
It intentionally shares the extension's scoring function (not its index scan).
"""
from __future__ import annotations

import contextlib
import os
import uuid
from collections import Counter

import asyncpg
import pytest

from app.services import sparse_encoder
from app.services.vector_store.pgvector import PgvectorStore

pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_VCHORD_TEST_DSN", "")
_VAULT = uuid.UUID(int=1)
_OTHER = uuid.UUID(int=2)


@contextlib.asynccontextmanager
async def _database(monkeypatch):
    if not _DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN is required for VectorChord coverage")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_vchord_stats_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = f"{_DSN.rsplit('/', 1)[0]}/{name}"
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        store = PgvectorStore(dsn=dsn, schema="vector_index", dense_dim=4,
                              sparse_shape="vchord")
        async with pool.acquire() as conn:
            await store._do_ensure(conn)
            version = await conn.fetchval(
                "SELECT extversion FROM pg_extension WHERE extname='vchord_bm25'"
            )
            assert version == "0.3.0", "Revalidate the scoring contract on pin changes"
            await conn.execute("""
                CREATE SEQUENCE bm25_term_id_seq;
                CREATE TABLE bm25_vocab (
                    term text PRIMARY KEY, term_id bigint UNIQUE NOT NULL,
                    df bigint NOT NULL DEFAULT 0,
                    updated_at timestamptz NOT NULL DEFAULT now()
                );
            """)
        async def get_pool():
            return pool
        monkeypatch.setattr(sparse_encoder, "get_pool", get_pool)
        monkeypatch.setattr(sparse_encoder.settings, "vector_store_driver", "pgvector")
        monkeypatch.setattr(sparse_encoder, "_stats_cache", None)
        yield store, pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _put(store, conn, ordinal, content, *, vault=_VAULT, kind="document"):
    indices, values = await sparse_encoder.encode_document(content, sparse_shape="vchord")
    counts = Counter(await sparse_encoder.tokenize(content))
    vocab = await sparse_encoder.lookup_term_ids(counts)
    assert dict(zip(indices, values)) == {vocab[t]: float(n) for t, n in counts.items()}
    await store.upsert_one(
        chunk_id=str(uuid.UUID(int=ordinal)), source_type=kind,
        source_id=str(uuid.UUID(int=ordinal)), vault_id=str(vault),
        section_path="", content=content, chunk_index=ordinal, dense=None,
        sparse_indices=indices, sparse_values=values, conn=conn,
    )


async def _seed(store, conn):
    for i in range(1, 25):
        await _put(store, conn, i, "공통 검색 " + "서울 " * (1 + i % 3)
                   + ("희귀은하 " if i <= 3 else "자료 ") + str(i),
                   vault=_VAULT if i <= 12 else _OTHER,
                   kind="table" if i % 4 == 0 else "document")
    # Rebuild over existing rows to exercise a built index, then later writes
    # exercise new versions without VACUUM discarding obsolete TIDs.
    await conn.execute("REINDEX INDEX vector_index.idx_vi_chunks_bm25")
    await conn.execute("ANALYZE vector_index.chunks")


_ORACLE = """
WITH authorized AS MATERIALIZED (
    SELECT chunk_id, sparse_bm25 FROM vector_index.chunks
    WHERE ($2::uuid[] IS NULL OR vault_id = ANY($2))
      AND ($3::text[] IS NULL OR source_type = ANY($3))
), scored AS MATERIALIZED (
    SELECT chunk_id::text AS id,
           sparse_bm25 <&> bm25_catalog.to_bm25query(
               'vector_index.idx_vi_chunks_bm25'::regclass,
               $1::int[]::bm25_catalog.bm25vector) AS score
    FROM authorized
)
SELECT id, score FROM scored WHERE score < 0 ORDER BY score, id
"""


async def _check(store, conn, query, *, vaults=None, kinds=None, limit=5):
    terms, weights = await sparse_encoder.encode_query(query, sparse_shape="vchord")
    assert terms and weights == [1.0] * len(terms)
    await conn.execute('SET search_path TO public, bm25_catalog')
    expected = await conn.fetch(_ORACLE, terms, vaults, kinds)
    hits = await store._search_sparse(
        conn, terms=terms, weights=weights, filter_uuids=vaults,
        filter_col="vault_id", source_type_values=kinds, limit=limit,
    )
    scores = {row["id"]: row["score"] for row in expected}
    assert len(hits) == min(limit, len(expected))
    assert len(set(hits)) == len(hits)
    assert set(hits) <= scores.keys()
    # Tie-aware top-k: all strictly better rows must be present; equal-score
    # boundary rows may appear in any order or subset.
    if hits:
        boundary = expected[len(hits) - 1]["score"]
        assert {r["id"] for r in expected if r["score"] < boundary} <= set(hits)
        assert all(scores[hit] <= boundary for hit in hits)
        assert [scores[h] for h in hits] == sorted(scores[h] for h in hits)
    return hits


async def test_kiwi_index_scoring_ignores_absent_and_stale_external_stats(monkeypatch):
    async with _database(monkeypatch) as (store, pool):
        async with pool.acquire() as conn:
            await _seed(store, conn)
            await conn.execute("SET enable_seqscan = off")
            await conn.execute("SET bm25_catalog.bm25_limit = 2")
            assert await conn.fetchval("SELECT to_regclass('bm25_stats')") is None
            before = dict(await conn.fetch("SELECT term, term_id FROM bm25_vocab"))
            baseline = {}
            for state in ("absent", "stale"):
                if state == "stale":
                    await conn.execute("""
                        CREATE TABLE bm25_stats (id int, total_docs bigint,
                            avgdl float8, k1 float8, b float8);
                        INSERT INTO bm25_stats VALUES (1, 999999999, 0.00001, 99, 0);
                        UPDATE bm25_vocab SET df = 999999999;
                    """)
                for query in ("희귀은하", "공통", "서울 서울"):
                    for vaults, kinds in ((None, None), ([_VAULT], None),
                                          ([_VAULT], ["document"]),
                                          (None, ["table"])):
                        key = (query, str(vaults), str(kinds))
                        hits = await _check(store, conn, query, vaults=vaults, kinds=kinds)
                        if state == "absent":
                            baseline[key] = hits
                        else:
                            assert hits == baseline[key]
            # Re-encoding existing terms and registering a new term preserves IDs.
            await sparse_encoder.encode_document("서울 신규어휘", sparse_shape="vchord")
            after = dict(await conn.fetch("SELECT term, term_id FROM bm25_vocab"))
            assert before.items() <= after.items()
            assert len(after) > len(before)
            terms, _ = await sparse_encoder.encode_query("서울", sparse_shape="vchord")
            await conn.execute("SET enable_seqscan = off")
            plan = await conn.fetch("""EXPLAIN SELECT chunk_id FROM vector_index.chunks
                ORDER BY sparse_bm25 <&> bm25_catalog.to_bm25query(
                    'vector_index.idx_vi_chunks_bm25'::regclass,
                    $1::int[]::bm25_catalog.bm25vector) LIMIT 5""", terms)
            assert "Index Scan using idx_vi_chunks_bm25" in "\n".join(r[0] for r in plan)


async def test_built_index_insert_update_delete_and_mvcc_match_exact_oracle(monkeypatch):
    async with _database(monkeypatch) as (store, pool):
        async with pool.acquire() as conn, pool.acquire() as writer:
            await _seed(store, conn)
            await conn.execute("SET enable_seqscan = off")
            await conn.execute("SET bm25_catalog.bm25_limit = 2")
            # Maintain a reader snapshot while a second session creates dead
            # index TIDs; both snapshots must obey visibility and vault ACLs.
            async with conn.transaction(isolation="repeatable_read"):
                original = await _check(store, conn, "희귀은하", vaults=[_VAULT], limit=10)
                await _put(store, writer, 30, "공통 서울 희귀은하 희귀은하")
                await _put(store, writer, 1, "공통 서울 수정 자료")
                await writer.execute("DELETE FROM vector_index.chunks WHERE chunk_id=$1",
                                     uuid.UUID(int=2))
                assert await _check(store, conn, "희귀은하", vaults=[_VAULT], limit=10) == original
            current = await _check(store, conn, "희귀은하", vaults=[_VAULT], limit=10)
            assert set(current) == {str(uuid.UUID(int=n)) for n in (3, 30)}
            for query in ("공통", "서울", "수정"):
                await _check(store, conn, query, vaults=[_VAULT], kinds=["document"])
            assert int(await conn.fetchval("SHOW bm25_catalog.bm25_limit")) == 2
