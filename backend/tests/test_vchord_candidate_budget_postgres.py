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
