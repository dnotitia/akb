"""Regression coverage for the VectorChord BM25 candidate budget.

The extension may stop an index-led scan after bm25_catalog.bm25_limit
candidates. SQL predicates applied above that scan can therefore remove every
candidate even when matching chunks exist beyond the budget. Materialising a
small ACL set first is deliberately the control case: its ranking does not
depend on the global BM25 candidate budget. The direct filtered query remains
the fast path for sealed segments; a growing scan or invisible row can still
make a finite page short, so only the -1 exact fallback proves exhaustion.

These cases require the extension-capable PostgreSQL image. They create one
database per test and skip when AKB_VCHORD_TEST_DSN is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.services.vector_store import pgvector as pgvector_module
from app.services.vector_store.pgvector import PgvectorStore

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_VCHORD_TEST_DSN", "")
_BUDGET = 2
_QUERY_TERM = 10
_RARE_TERM = 7_777
_UNKNOWN_TERM = 9_999_999


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


@contextlib.asynccontextmanager
async def _store() -> AsyncIterator[tuple[PgvectorStore, asyncpg.Pool]]:
    if not _DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN is required for VectorChord coverage")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_vchord_budget_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=4)
    try:
        store = PgvectorStore(
            dsn=_database_dsn(name),
            schema="vector_index",
            dense_dim=4,
            sparse_shape="vchord",
        )
        async with pool.acquire() as conn:
            await store._do_ensure(conn)
        yield store, pool
    finally:
        await pool.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@contextlib.asynccontextmanager
async def _candidate_budget(
    conn: asyncpg.Connection,
    *,
    budget: int = _BUDGET,
) -> AsyncIterator[None]:
    # Small isolated tables otherwise choose a sequential scan, which cannot
    # exercise the extension's candidate budget. The product's index-led SQL
    # branches remain unchanged; this makes their physical index path explicit.
    await conn.execute("SET enable_seqscan = off")
    await conn.execute(f"SET bm25_catalog.bm25_limit = {budget}")
    actual = await conn.fetchval("SHOW bm25_catalog.bm25_limit")
    assert int(actual) == budget
    assert await conn.fetchval("SHOW enable_seqscan") == "off"
    try:
        yield
    finally:
        await conn.execute("RESET bm25_catalog.bm25_limit")
        await conn.execute("RESET enable_seqscan")


async def _constant(value: bool) -> bool:
    return value


async def _put(
    store: PgvectorStore,
    conn: asyncpg.Connection,
    *,
    chunk_id: uuid.UUID,
    vault_id: uuid.UUID,
    source_type: str,
    terms: list[int],
    chunk_index: int,
) -> None:
    await store.upsert_one(
        chunk_id=str(chunk_id),
        source_type=source_type,
        source_id=str(chunk_id),
        vault_id=str(vault_id),
        section_path="",
        content=f"chunk-{chunk_index}",
        chunk_index=chunk_index,
        dense=None,
        sparse_indices=terms,
        sparse_values=[1.0] * len(terms),
        conn=conn,
    )


async def _seed(
    store: PgvectorStore,
    conn: asyncpg.Connection,
) -> tuple[uuid.UUID, set[str], set[str]]:
    """Write low-ranked in-scope matches behind high-ranked distractors."""
    target_vault = uuid.UUID(int=1)
    other_vault = uuid.UUID(int=2)
    scoped_ids: set[str] = set()
    distractor_ids: set[str] = set()

    # Each target contains the query term once and grows longer with its ID.
    # Each distractor repeats the term many times and is therefore safely ahead
    # of every target in the BM25 order, while being outside the ACL and type.
    for ordinal in range(1, 6):
        chunk_id = uuid.UUID(int=ordinal)
        scoped_ids.add(str(chunk_id))
        await _put(
            store,
            conn,
            chunk_id=chunk_id,
            vault_id=target_vault,
            source_type="document",
            terms=[_QUERY_TERM] + list(range(1000, 1000 + ordinal)),
            chunk_index=ordinal,
        )
    for ordinal in range(101, 131):
        chunk_id = uuid.UUID(int=ordinal)
        distractor_ids.add(str(chunk_id))
        await _put(
            store,
            conn,
            chunk_id=chunk_id,
            vault_id=other_vault,
            source_type="table",
            terms=[_QUERY_TERM] * 32,
            chunk_index=ordinal,
        )

    await conn.execute("ANALYZE vector_index.chunks")
    return target_vault, scoped_ids, distractor_ids


async def _acl_hits(
    store: PgvectorStore,
    conn: asyncpg.Connection,
    vault_id: uuid.UUID,
    *,
    selective: bool,
    terms: list[int] | None = None,
) -> list[str]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            type(store),
            "_filter_is_selective",
            lambda *args, **kwargs: _constant(selective),
        )
        return await store._search_sparse(
            conn,
            terms=terms or [_QUERY_TERM],
            weights=[1.0],
            filter_uuids=[vault_id],
            filter_col="vault_id",
            limit=5,
        )


async def test_low_budget_preserves_materialized_acl_matches():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            target_vault, scoped_ids, _ = await _seed(store, conn)
            async with _candidate_budget(conn):
                hits = await _acl_hits(
                    store, conn, target_vault, selective=True,
                )

    assert set(hits) == scoped_ids


async def test_low_budget_preserves_index_led_acl_matches():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            target_vault, scoped_ids, _ = await _seed(store, conn)
            async with _candidate_budget(conn):
                hits = await _acl_hits(
                    store, conn, target_vault, selective=False,
                )

    assert set(hits) == scoped_ids


async def test_low_budget_preserves_source_type_matches():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            _, scoped_ids, _ = await _seed(store, conn)
            async with _candidate_budget(conn):
                hits = await store._search_sparse(
                    conn,
                    terms=[_QUERY_TERM],
                    weights=[1.0],
                    filter_uuids=None,
                    filter_col="vault_id",
                    source_type_values=["document"],
                    limit=5,
                )

    assert set(hits) == scoped_ids


async def test_low_budget_fills_requested_unfiltered_top_k():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            _, _, distractor_ids = await _seed(store, conn)
            async with _candidate_budget(conn):
                hits = await store._search_sparse(
                    conn,
                    terms=[_QUERY_TERM],
                    weights=[1.0],
                    filter_uuids=None,
                    filter_col="vault_id",
                    limit=5,
                )

    assert len(hits) == 5
    assert set(hits).issubset(distractor_ids)


@contextlib.asynccontextmanager
async def _record_budget_calls() -> AsyncIterator[list[int]]:
    calls: list[int] = []
    original = pgvector_module._set_vchord_candidate_budget

    async def record(conn: asyncpg.Connection, budget: int) -> None:
        calls.append(budget)
        await original(conn, budget)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            pgvector_module,
            "_set_vchord_candidate_budget",
            record,
        )
        yield calls


@contextlib.asynccontextmanager
async def _record_queries(
    conn: asyncpg.Connection,
) -> AsyncIterator[list[tuple[str, tuple[object, ...]]]]:
    queries: list[tuple[str, tuple[object, ...]]] = []

    def record(entry: object) -> None:
        query = getattr(entry, "query", None)
        args = getattr(entry, "args", ())
        if isinstance(query, str):
            queries.append((query, tuple(args or ())))

    conn.add_query_logger(record)
    try:
        yield queries
    finally:
        await asyncio.sleep(0)
        conn.remove_query_logger(record)


async def _assert_bm25_index_scan(
    conn: asyncpg.Connection,
    sql: str,
    args: tuple[object, ...],
    *,
    budget: int,
) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
        await conn.execute(
            'SET LOCAL search_path TO "$user", public, bm25_catalog'
        )
        await conn.execute(
            f"SET LOCAL bm25_catalog.bm25_limit = {budget}"
        )
        rows = await conn.fetch(f"EXPLAIN {sql}", *args)
    plan = "\n".join(str(row[0]) for row in rows)
    assert "Index Scan using idx_vi_chunks_bm25" in plan


async def _seed_rare(
    store: PgvectorStore,
    conn: asyncpg.Connection,
) -> tuple[uuid.UUID, set[str]]:
    target_vault = uuid.UUID(int=3)
    scoped_ids: set[str] = set()
    for ordinal in range(1, 3):
        chunk_id = uuid.UUID(int=ordinal)
        scoped_ids.add(str(chunk_id))
        await _put(
            store,
            conn,
            chunk_id=chunk_id,
            vault_id=target_vault,
            source_type="document",
            terms=[_RARE_TERM] + list(range(2_000, 2_000 + ordinal)),
            chunk_index=ordinal,
        )
    await conn.execute("ANALYZE vector_index.chunks")
    return target_vault, scoped_ids


async def test_index_led_acl_adapts_and_restores_pooled_budget():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            target_vault, scoped_ids, _ = await _seed(store, conn)
            async with _record_queries(conn) as queries:
                async with _record_budget_calls() as budgets:
                    async with _candidate_budget(conn):
                        hits = await _acl_hits(
                            store, conn, target_vault, selective=False,
                        )
                        restored = int(
                            await conn.fetchval(
                                "SHOW bm25_catalog.bm25_limit"
                            )
                        )

            probes = [
                query for query in queries
                if "global_candidates AS MATERIALIZED" in query[0]
            ]
            assert set(hits) == scoped_ids
            assert budgets == [5, 20, 80]
            assert len(probes) == 3
            await _assert_bm25_index_scan(
                conn, *probes[-1], budget=budgets[-1],
            )
    assert restored == _BUDGET


async def test_source_type_filter_adapts_candidate_budget():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            _, scoped_ids, _ = await _seed(store, conn)
            async with _record_budget_calls() as budgets:
                async with _candidate_budget(conn):
                    hits = await store._search_sparse(
                        conn,
                        terms=[_QUERY_TERM],
                        weights=[1.0],
                        filter_uuids=None,
                        filter_col="vault_id",
                        source_type_values=["document"],
                        limit=5,
                    )

    assert set(hits) == scoped_ids
    assert budgets == [5, 20, 80]


async def test_filtered_short_page_uses_exact_fallback_in_order():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            target_vault, scoped_ids = await _seed_rare(store, conn)
            async with _record_budget_calls() as budgets:
                async with _candidate_budget(conn):
                    hits = await _acl_hits(
                        store,
                        conn,
                        target_vault,
                        selective=False,
                        terms=[_RARE_TERM],
                    )
            async with _candidate_budget(conn, budget=-1):
                expected = await _acl_hits(
                    store,
                    conn,
                    target_vault,
                    selective=False,
                    terms=[_RARE_TERM],
                )

    assert hits == expected
    assert set(hits) == scoped_ids
    assert budgets == [5, -1]


async def test_unknown_terms_use_exact_empty_fallback():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            await _seed(store, conn)
            async with _record_budget_calls() as budgets:
                async with _candidate_budget(conn):
                    hits = await store._search_sparse(
                        conn,
                        terms=[_UNKNOWN_TERM],
                        weights=[1.0],
                        filter_uuids=None,
                        filter_col="vault_id",
                        limit=5,
                    )

    assert hits == []
    assert budgets == [5, -1]


async def test_zero_configured_budget_does_not_override_requested_top_k():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            _, _, distractor_ids = await _seed(store, conn)
            async with _record_budget_calls() as budgets:
                async with _candidate_budget(conn, budget=0):
                    hits = await store._search_sparse(
                        conn,
                        terms=[_QUERY_TERM],
                        weights=[1.0],
                        filter_uuids=None,
                        filter_col="vault_id",
                        limit=5,
                    )

    assert len(hits) == 5
    assert set(hits).issubset(distractor_ids)
    assert budgets == [5]


async def test_overlarge_page_uses_exact_budget():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            _, scoped_ids, distractor_ids = await _seed(store, conn)
            async with _record_budget_calls() as budgets:
                async with _candidate_budget(conn):
                    hits = await store._search_sparse(
                        conn,
                        terms=[_QUERY_TERM],
                        weights=[1.0],
                        filter_uuids=None,
                        filter_col="vault_id",
                        limit=65_536,
                    )

    assert set(hits) == scoped_ids | distractor_ids
    assert budgets == [-1]


async def test_configured_exact_budget_stays_on_direct_filtered_path():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            target_vault, scoped_ids, _ = await _seed(store, conn)
            async with _record_budget_calls() as budgets:
                async with _candidate_budget(conn, budget=-1):
                    hits = await _acl_hits(
                        store, conn, target_vault, selective=False,
                    )
                    restored = int(
                        await conn.fetchval(
                            "SHOW bm25_catalog.bm25_limit"
                        )
                    )

    assert set(hits) == scoped_ids
    assert budgets == []
    assert restored == -1


async def test_local_budget_restores_after_error():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            await _seed(store, conn)
            original = pgvector_module._set_vchord_candidate_budget

            async def fail_after_set(
                connection: asyncpg.Connection,
                budget: int,
            ) -> None:
                await original(connection, budget)
                raise RuntimeError("injected candidate-budget failure")

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    pgvector_module,
                    "_set_vchord_candidate_budget",
                    fail_after_set,
                )
                async with _candidate_budget(conn):
                    with pytest.raises(
                        RuntimeError,
                        match="injected candidate-budget failure",
                    ):
                        await store._search_sparse(
                            conn,
                            terms=[_QUERY_TERM],
                            weights=[1.0],
                            filter_uuids=None,
                            filter_col="vault_id",
                            limit=5,
                        )
                    restored = int(
                        await conn.fetchval(
                            "SHOW bm25_catalog.bm25_limit"
                        )
                    )

    assert restored == _BUDGET


async def test_dead_index_entries_use_exact_unfiltered_fallback():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            _, scoped_ids, distractor_ids = await _seed(store, conn)
            # Keep obsolete high-scoring index TIDs until the scan. No VACUUM:
            # this is the visibility case where a raw finite page is not proof
            # that all live matches have been examined.
            await conn.execute(
                'DELETE FROM vector_index.chunks '
                'WHERE chunk_id = ANY($1::uuid[])',
                [uuid.UUID(chunk_id) for chunk_id in distractor_ids],
            )
            async with _record_budget_calls() as budgets:
                async with _candidate_budget(conn):
                    hits = await store._search_sparse(
                        conn,
                        terms=[_QUERY_TERM],
                        weights=[1.0],
                        filter_uuids=None,
                        filter_col="vault_id",
                        limit=5,
                    )

    assert set(hits) == scoped_ids
    assert budgets == [5, -1]


@pytest.mark.parametrize("mode", ["unfiltered", "filtered", "configured-exact", "large-k", "materialized"])
async def test_broad_exact_search_fails_closed_without_unbounded_scan(mode):
    """Planner selectivity and operator -1 cannot bypass the exact row cap."""
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            vault, _, _ = await _seed(store, conn)
            original_plan = await conn.fetchval("SHOW plan_cache_mode")
            original_timeout = await conn.fetchval("SHOW statement_timeout")
            original_path = await conn.fetchval("SHOW search_path")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(pgvector_module, "_VCHORD_MAX_EXACT_ROWS", 4)
                mp.setattr(type(store), "_filter_is_selective",
                           lambda *a, **k: _constant(mode == "materialized"))
                async with _record_budget_calls() as budgets:
                    async with _candidate_budget(conn, budget=-1 if mode == "configured-exact" else 2):
                        with pytest.raises(pgvector_module.VectorStoreUnavailable, match="bounded row budget"):
                            await store._search_sparse(
                                conn, terms=[_UNKNOWN_TERM], weights=[1.0],
                                filter_uuids=[vault] if mode in ("filtered", "materialized") else None,
                                filter_col="vault_id", limit=65_536 if mode == "large-k" else 5,
                            )
                        assert -1 not in budgets
                        assert await conn.fetchval("SELECT 1") == 1
            assert await conn.fetchval("SHOW plan_cache_mode") == original_plan
            assert await conn.fetchval("SHOW statement_timeout") == original_timeout
            assert await conn.fetchval("SHOW search_path") == original_path


async def test_small_materialized_scope_survives_large_global_corpus():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            vault, scoped, _ = await _seed(store, conn)
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(pgvector_module, "_VCHORD_MAX_EXACT_ROWS", 5)
                hits = await _acl_hits(store, conn, vault, selective=True)
            assert set(hits) == scoped


async def test_selective_scope_over_the_exact_cap_is_answered_index_led():
    """Which shape runs decides latency, never the rows (akb#626).

    A scope the estimator calls selective is materialised, and materialising is
    exact work, so the cap bounds it. A scope over the cap used to raise, and
    with no finite candidates to keep, the whole sparse leg came back empty —
    while the index-led shape answers the same scope. In production that is
    every scope over 10,000 rows and under 1% of the corpus.

    The estimator is the real one: 30 scoped rows among 6,000 is 0.5%, so
    `_filter_is_selective` says yes from `pg_stats` unpatched. Only the cap is
    scaled down, to 20, to put the scope over it.
    """
    scoped_rows = 30
    target = uuid.UUID(int=0xA)
    other = uuid.UUID(int=0xB)
    scoped_ids: set[str] = set()
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            # Distinct lengths give distinct scores, so the top-k is one answer
            # rather than a choice among ties.
            for ordinal in range(1, scoped_rows + 1):
                chunk_id = uuid.UUID(int=0x1000 + ordinal)
                scoped_ids.add(str(chunk_id))
                await _put(
                    store,
                    conn,
                    chunk_id=chunk_id,
                    vault_id=target,
                    source_type="document",
                    terms=[_QUERY_TERM] + list(range(1000, 1000 + ordinal)),
                    chunk_index=ordinal,
                )
            await conn.execute("""
                INSERT INTO vector_index.chunks
                    (chunk_id, source_type, source_id, vault_id,
                     section_path, content, chunk_index, sparse_bm25)
                SELECT md5(i::text)::uuid, 'document', md5(i::text)::uuid,
                       $1, '', 'other', i, '{20:1}'::bm25_catalog.bm25vector
                FROM generate_series(1, $2::int) i
            """, other, 6_000 - scoped_rows)
            await conn.execute("REINDEX INDEX vector_index.idx_vi_chunks_bm25")
            await conn.execute("ANALYZE vector_index.chunks")
            assert await store._filter_is_selective(conn, "vault_id", [target]) is True

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(pgvector_module, "_VCHORD_MAX_EXACT_ROWS", 20)
                async with _candidate_budget(conn, budget=100):
                    chosen = await store._search_sparse(
                        conn, terms=[_QUERY_TERM], weights=[1.0],
                        filter_uuids=[target], filter_col="vault_id", limit=10,
                    )
                    mp.setattr(type(store), "_filter_is_selective",
                               lambda *a, **k: _constant(False))
                    index_led = await store._search_sparse(
                        conn, terms=[_QUERY_TERM], weights=[1.0],
                        filter_uuids=[target], filter_col="vault_id", limit=10,
                    )

    assert len(chosen) == 10
    assert set(chosen) <= scoped_ids
    assert chosen == index_led


async def test_exact_probe_timeout_restores_connection_and_stricter_server_budget():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            await _seed(store, conn)
            original = type(conn).fetchval
            observed_timeouts = []

            async def slow_probe(self, query, *args, **kwargs):
                if "bounded_exact_scope" in query:
                    observed_timeouts.append(await original(self, "SHOW statement_timeout"))
                    await self.execute("SELECT pg_sleep(1)")
                return await original(self, query, *args, **kwargs)

            await conn.execute("SET statement_timeout = '20ms'")
            try:
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(type(conn), "fetchval", slow_probe)
                    with pytest.raises((asyncpg.QueryCanceledError, TimeoutError)):
                        await store._search_sparse(
                            conn, terms=[_UNKNOWN_TERM], weights=[1.0],
                            filter_uuids=None, filter_col="vault_id", limit=65_536,
                        )
                assert observed_timeouts == ["20ms"]
                assert await conn.fetchval("SHOW statement_timeout") == "20ms"
                assert await conn.fetchval("SELECT 1") == 1
            finally:
                await conn.execute("RESET statement_timeout")


@pytest.mark.parametrize("phase", ["estimate", "ranking"])
async def test_caller_deadline_cancels_and_restores_connection(phase):
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            vault, _, _ = await _seed(store, conn)
            before = {key: await conn.fetchval(f"SHOW {key}") for key in (
                "statement_timeout", "plan_cache_mode", "search_path", "bm25_catalog.bm25_limit",
            )}
            original = type(conn).fetchval

            async def delayed(self, query, *args, **kwargs):
                if "bounded_exact_scope" in query:
                    # Simulate client-side delay after SET LOCAL; the wall
                    # deadline must roll back even with no server timeout.
                    await asyncio.sleep(1)
                return await original(self, query, *args, **kwargs)

            async def slow_estimate(*args, **kwargs):
                await asyncio.sleep(1)
                return False

            with pytest.MonkeyPatch.context() as mp:
                if phase == "estimate":
                    mp.setattr(type(store), "_filter_is_selective", slow_estimate)
                else:
                    mp.setattr(type(conn), "fetchval", delayed)
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0.05):
                        await store._search_sparse(
                            conn, terms=[_UNKNOWN_TERM], weights=[1.0],
                            filter_uuids=[vault] if phase == "estimate" else None,
                            filter_col="vault_id", limit=65_536,
                        )
            assert await conn.fetchval("SELECT 1") == 1
            for key, value in before.items():
                assert await conn.fetchval(f"SHOW {key}") == value


async def test_real_large_corpus_keeps_finite_index_search_but_refuses_exact():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO vector_index.chunks
                    (chunk_id, source_type, source_id, vault_id,
                     section_path, content, chunk_index, sparse_bm25)
                SELECT md5(i::text)::uuid, 'document', md5(i::text)::uuid,
                       $1, '', 'common term', i, '{10:1}'::bm25_catalog.bm25vector
                FROM generate_series(1, $2::int) i
            """, uuid.UUID(int=1), pgvector_module._VCHORD_MAX_EXACT_ROWS + 1)
            await conn.execute("REINDEX INDEX vector_index.idx_vi_chunks_bm25")
            await conn.execute("ANALYZE vector_index.chunks")
            async with _candidate_budget(conn, budget=5):
                common = await store._search_sparse(
                    conn, terms=[10], weights=[1.0], filter_uuids=None,
                    filter_col="vault_id", limit=5,
                )
                assert len(common) == 5
                async with _record_budget_calls() as budgets:
                    with pytest.raises(pgvector_module.VectorStoreUnavailable, match="bounded row budget"):
                        await store._search_sparse(
                            conn, terms=[_UNKNOWN_TERM], weights=[1.0], filter_uuids=None,
                            filter_col="vault_id", limit=5,
                        )
                assert -1 not in budgets
                assert int(await conn.fetchval("SHOW bm25_catalog.bm25_limit")) == 5


async def test_finite_index_search_can_exceed_five_seconds_under_caller_budget():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            await _seed(store, conn)
            original = type(conn).fetch
            seen = []

            async def slow_index(self, query, *args, **kwargs):
                if "SELECT chunk_id FROM (" in query and "sparse_bm25" in query:
                    seen.append(await self.fetchval("SHOW statement_timeout"))
                    await self.execute("SELECT pg_sleep(5.1)")
                return await original(self, query, *args, **kwargs)

            await conn.execute("SET statement_timeout = '10s'")
            try:
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(type(conn), "fetch", slow_index)
                    async with _candidate_budget(conn, budget=5):
                        hits = await store._search_sparse(
                            conn, terms=[_QUERY_TERM], weights=[1.0],
                            filter_uuids=None, filter_col="vault_id", limit=1,
                        )
                assert len(hits) == 1
                assert seen == ["10s"]
                assert await conn.fetchval("SHOW statement_timeout") == "10s"
            finally:
                await conn.execute("RESET statement_timeout")


@pytest.mark.parametrize("dense_first", [True, False])
async def test_exact_refusal_keeps_dense_and_scoped_sparse_hits(dense_first):
    from app.services.vector_store.base import VectorSearchDegraded

    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            vault, scoped, distractors = await _seed(store, conn)
            dense_id = str(uuid.UUID(int=50))
            await store.upsert_one(
                conn=conn, chunk_id=dense_id, source_type="document", source_id=dense_id,
                vault_id=str(vault), section_path="", content="dense-only match", chunk_index=50,
                dense=[1.0, 0.0, 0.0, 0.0], sparse_indices=[_RARE_TERM], sparse_values=[1.0],
            )
            # Out-of-scope vectors must not leak into retained dense results.
            await conn.execute("UPDATE vector_index.chunks SET dense='[1,0,0,0]'::vector "
                               "WHERE vault_id=$1", uuid.UUID(int=2))

        dense_done, sparse_done = asyncio.Event(), asyncio.Event()
        real_dense, real_sparse = store._search_dense, store._search_sparse

        async def dense(*args, **kwargs):
            if not dense_first:
                await sparse_done.wait()
            try:
                return await real_dense(*args, **kwargs)
            finally:
                dense_done.set()

        async def sparse(*args, **kwargs):
            if dense_first:
                await dense_done.wait()
            try:
                return await real_sparse(*args, **kwargs)
            finally:
                sparse_done.set()

        async def get_pool():
            return pool

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store, "_pool", get_pool)
            mp.setattr(store, "_search_dense", dense)
            mp.setattr(store, "_search_sparse", sparse)
            mp.setattr(pgvector_module, "_VCHORD_MAX_EXACT_ROWS", 4)
            with pytest.raises(VectorSearchDegraded) as caught:
                await store.hybrid_search(
                    query_text="term", query_dense=[1.0, 0.0, 0.0, 0.0],
                    query_sparse_indices=[_QUERY_TERM], query_sparse_values=[1.0],
                    source_ids=None, vault_ids=[str(vault)], source_types=["document"],
                    limit=20, prefetch_per_leg=50,
                )
        result = caught.value
        assert result.reason == "sparse_search_budget_exceeded"
        assert {hit.chunk_id for hit in result.hits} == scoped | {dense_id}
        assert not ({hit.chunk_id for hit in result.hits} & distractors)
        assert dense_done.is_set() and sparse_done.is_set()
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1


async def test_exact_refusal_without_dense_keeps_finite_scoped_candidates():
    from app.services.vector_store.base import VectorSearchDegraded

    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            vault, scoped, _ = await _seed(store, conn)

        async def get_pool():
            return pool

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store, "_pool", get_pool)
            mp.setattr(pgvector_module, "_VCHORD_MAX_EXACT_ROWS", 4)
            with pytest.raises(VectorSearchDegraded) as caught:
                await store.hybrid_search(
                    query_text="term", query_dense=None,
                    query_sparse_indices=[_QUERY_TERM], query_sparse_values=[1.0],
                    source_ids=None, vault_ids=[str(vault)], source_types=["document"],
                    limit=20, prefetch_per_leg=50,
                )
        assert caught.value.reason == "sparse_search_budget_exceeded"
        assert {hit.chunk_id for hit in caught.value.hits} == scoped
