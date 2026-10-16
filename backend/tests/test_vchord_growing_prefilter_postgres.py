"""The bundled extension must prefilter growing rows before they enter top-k.

These tests deliberately inspect the *finite* scan, without the backend's
correctness fallback hiding its cost. Each creates a disposable database via
the same AKB_VCHORD_TEST_DSN contract as the other extension regressions.
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.test_vchord_candidate_budget_postgres import (
    _assert_bm25_index_scan,
    _candidate_budget,
    _record_budget_calls,
    _store,
)

pytestmark = pytest.mark.asyncio

_VAULT = uuid.UUID(int=1)
_SOURCE = uuid.UUID(int=2)
_OTHER = uuid.UUID(int=3)
_SQL = """
    SELECT chunk_id, score FROM (
        SELECT c.chunk_id::text AS chunk_id,
               c.sparse_bm25 <&> bm25_catalog.to_bm25query(
                   'vector_index.idx_vi_chunks_bm25'::regclass,
                   '{{10:1}}'::bm25_catalog.bm25vector) AS score
          FROM vector_index.chunks c
         WHERE c.sparse_bm25 IS NOT NULL {predicate}
         ORDER BY score LIMIT 5
    ) ranked WHERE score < 0 ORDER BY score
"""


async def _insert(conn, first, last, *, distractor=False):
    rows = []
    for i in range(first, last):
        # Unique lengths avoid relying on the extension's tie ordering.
        vector = "{10:32}" if distractor else "{10:1," + f"{100 + i}:{i + 1}" + "}"
        rows.append((uuid.UUID(int=1000 + i), _OTHER if distractor else _VAULT,
                     _OTHER if distractor else _SOURCE, "table" if distractor else "document", i, vector))
    await conn.executemany("""
        INSERT INTO vector_index.chunks
            (chunk_id, vault_id, source_id, source_type, section_path, content, chunk_index, sparse_bm25)
        VALUES ($1,$2,$3,$4,'','canary',$5,$6::bm25_catalog.bm25vector)
    """, rows)


async def _seed(conn):
    await _insert(conn, 0, 3)
    await conn.execute("REINDEX INDEX vector_index.idx_vi_chunks_bm25")
    await _insert(conn, 3, 6)
    await _insert(conn, 10, 40, distractor=True)
    await conn.execute("ANALYZE vector_index.chunks")
    meta = await conn.fetchval("""
        SELECT bm25_catalog.bm25_page_inspect('vector_index.idx_vi_chunks_bm25'::regclass, 0)
    """)
    assert "sealed_doc_id: 3," in meta  # Both segment types really participate.


async def _ranked(conn, predicate, *, budget, prefilter=True):
    async with conn.transaction():
        await conn.execute('SET LOCAL search_path TO "$user", public, bm25_catalog')
        await conn.execute("SET LOCAL plan_cache_mode=force_custom_plan")
        await conn.execute(f"SET LOCAL bm25_catalog.bm25_limit={budget}")
        await conn.execute(f"SET LOCAL bm25_catalog.enable_prefilter={'on' if prefilter else 'off'}")
        return await conn.fetch(_SQL.format(predicate=predicate))


@pytest.mark.parametrize("scope,predicate", [
    ("vault", f"AND vault_id='{_VAULT}'::uuid"),
    ("source", f"AND source_id='{_SOURCE}'::uuid"),
    ("type", "AND source_type='document'"),
])
async def test_growing_distractors_do_not_displace_authorized_finite_results(scope, predicate):
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            await _seed(conn)
            async with _candidate_budget(conn, budget=5):
                await _assert_bm25_index_scan(conn, _SQL.format(predicate=predicate), (), budget=5)
                expected = await _ranked(conn, predicate, budget=-1)
                # This control proves the data actually exercise prefiltering.
                assert await _ranked(conn, predicate, budget=5, prefilter=False) == []
                bounded = await _ranked(conn, predicate, budget=5)
                assert len(bounded) == 5, "growing rows exhausted the finite page before ACL/type filtering"
                assert [r["chunk_id"] for r in bounded] == [r["chunk_id"] for r in expected]
                assert [r["score"] for r in bounded] == pytest.approx([r["score"] for r in expected])
                # Exercise the product route too, not just a hand-written SQL.
                with pytest.MonkeyPatch.context() as mp:
                    async def nonselective(*_args, **_kwargs):
                        return False
                    mp.setattr(type(store), "_filter_is_selective", nonselective)
                    async with _record_budget_calls() as budgets:
                        hits = await store._search_sparse(
                            conn, terms=[10], weights=[1.0],
                            filter_uuids=[_VAULT] if scope == "vault" else [_SOURCE] if scope == "source" else None,
                            filter_col="source_id" if scope == "source" else "vault_id",
                            source_type_values=["document"] if scope == "type" else None, limit=5,
                        )
                assert hits == [r["chunk_id"] for r in expected]
                assert budgets == [5], "a full authorized finite page should not enumerate all postings"


@pytest.mark.parametrize("visibility", ["deleted", "uncommitted", "old-snapshot"])
async def test_invisible_growing_tids_do_not_raise_the_pruning_threshold(visibility):
    async with _store() as (_, pool):
        async with pool.acquire() as conn, pool.acquire() as writer:
            await _insert(conn, 0, 6)
            await conn.execute("REINDEX INDEX vector_index.idx_vi_chunks_bm25")
            if visibility == "deleted":
                await _insert(writer, 10, 40, distractor=True)
                await writer.execute("DELETE FROM vector_index.chunks WHERE vault_id=$1", _OTHER)
            async with conn.transaction(isolation="repeatable_read"):
                await conn.fetchval("SELECT count(*) FROM vector_index.chunks")
                tx = writer.transaction()
                await tx.start()
                try:
                    if visibility != "deleted":
                        await _insert(writer, 10, 40, distractor=True)
                    if visibility != "uncommitted":
                        await tx.commit()
                        tx = None
                    async with _candidate_budget(conn, budget=5):
                        expected = await _ranked(conn, "", budget=-1)
                        bounded = await _ranked(conn, "", budget=5)
                        assert len(bounded) == 5, "invisible growing TIDs displaced visible sealed matches"
                        assert [r["chunk_id"] for r in bounded] == [r["chunk_id"] for r in expected]
                        assert [r["score"] for r in bounded] == pytest.approx([r["score"] for r in expected])
                        plan = json.loads(await conn.fetchval(
                            "EXPLAIN (FORMAT JSON) " + _SQL.format(predicate="")))
                        assert "idx_vi_chunks_bm25" in json.dumps(plan)
                finally:
                    if tx is not None:
                        await tx.rollback()


async def test_growing_prefilter_uses_the_snapshot_visible_hot_tuple():
    async with _store() as (_, pool):
        async with pool.acquire() as conn, pool.acquire() as writer:
            await _seed(conn)
            predicate = "AND source_type='document'"
            async with conn.transaction(isolation="repeatable_read"):
                await conn.fetchval("SELECT count(*) FROM vector_index.chunks")
                # source_type is not indexed: leave a HOT chain with different
                # qual results for the old snapshot and a new request.
                await writer.execute("UPDATE vector_index.chunks SET source_type='document' WHERE vault_id=$1", _OTHER)
                async with _candidate_budget(conn, budget=5):
                    expected = await _ranked(conn, predicate, budget=-1)
                    bounded = await _ranked(conn, predicate, budget=5)
                    assert len(bounded) == 5
                    assert [r['chunk_id'] for r in bounded] == [r['chunk_id'] for r in expected]
            async with _candidate_budget(conn, budget=5):
                bounded = await _ranked(conn, predicate, budget=5)
                expected = await _ranked(conn, predicate, budget=-1)
                assert len(bounded) == 5
                assert all(uuid.UUID(r['chunk_id']).int >= 1010 for r in bounded)
                assert [r['score'] for r in bounded] == pytest.approx([r['score'] for r in expected])


async def test_no_overlap_growing_rows_do_not_evaluate_heap_quals():
    async with _store() as (_, pool):
        async with pool.acquire() as conn:
            await _seed(conn)
            await conn.execute("CREATE SEQUENCE growing_heap_checks")
            await conn.execute("""
                CREATE FUNCTION count_growing_qual() RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
                BEGIN PERFORM nextval('growing_heap_checks'); RETURN TRUE; END $$
            """)
            sql = _SQL.format(predicate="AND count_growing_qual()").replace("'{10:1}'", "'{999999:1}'")
            async with _candidate_budget(conn, budget=5):
                await _assert_bm25_index_scan(conn, sql, (), budget=5)
                async with conn.transaction():
                    await conn.execute('SET LOCAL search_path TO "$user", public, bm25_catalog')
                    assert await conn.fetch(sql) == []
                # A check-before-score patch would call the qual for every
                # growing row despite every score being zero.
                assert await conn.fetchval("SELECT is_called FROM growing_heap_checks") is False


async def test_growing_search_observes_upsert_and_delete_without_reindex():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            await _seed(conn)
            chunk = str(uuid.UUID(int=1000))
            await store.upsert_one(
                chunk_id=chunk, source_type="document", source_id=str(_SOURCE), vault_id=str(_VAULT),
                section_path="", content="rewritten", chunk_index=0, dense=None,
                sparse_indices=[777], sparse_values=[1.0], conn=conn,
            )
            async with _candidate_budget(conn, budget=5):
                async def search(term):
                    return await store._search_sparse(conn, terms=[term], weights=[1.0],
                        filter_uuids=[_VAULT], filter_col="vault_id", limit=5)
                assert await search(777) == [chunk]
                assert chunk not in await search(10)
                await store.delete_point(chunk, conn=conn)
                assert await search(777) == []
