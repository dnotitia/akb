"""The `vchord` shape against a PostgreSQL that actually has the extension.

The unit tests beside this one hold the weight convention and the exhaustive
dispatch, and they are the right place for those. What they cannot see is
everything this shape's value rests on: whether the DDL produces an index the
planner will use, whether a written row is retrievable, and whether the query
branch that measurement chose is the branch that runs.

Mutating the implementation showed exactly that gap — deleting the BM25 index
from the DDL, forcing the selectivity decision to one answer, and removing the
materialised branch all left the unit suite green. A guard nothing can break is
not a guard.

Needs an extension-capable database, which the stock `pgvector/pgvector` image
is not. `deploy/postgres/Dockerfile` builds one. Point `AKB_VCHORD_TEST_DSN` at
it; absent that, these skip — and they say so rather than passing.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import asyncpg
import pytest

from app.services.vector_store import VectorStoreUnavailable
from app.services.vector_store.pgvector import PgvectorStore, _bm25vector_literal

pytestmark = pytest.mark.asyncio  # 동기 테스트는 아래에서 개별 해제

_DSN = os.environ.get("AKB_VCHORD_TEST_DSN", "")


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


@contextlib.asynccontextmanager
async def _store():
    if not _DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN 미설정 — 확장이 있는 PostgreSQL 이 필요하다")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_vchord_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=4)
    try:
        store = PgvectorStore(
            dsn=_database_dsn(name), schema="vector_index",
            dense_dim=4, sparse_shape="vchord",
        )
        async with pool.acquire() as conn:
            await store._do_ensure(conn)
        yield store, pool
    finally:
        await pool.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_the_vector_literal_is_strictly_increasing_and_folded():
    """Ordering is a correctness property — and so is folding.

    The first version of this assertion expected `{1:2, 5:1, 5:1}` and passed,
    because it agreed with the bug beside it. PostgreSQL did not: "Indexes are
    not increasing at position 9". Sorted is not enough; a repeated id has to
    become one entry."""
    assert _bm25vector_literal([5, 1, 5], [1.0, 2.0, 1.0]) == "{1:2, 5:2}"
    # No terms is a vector, not an absence. `NULL` is reserved for "nothing has
    # encoded this row yet", which is the only thing that makes a backfill's
    # `IS NULL` an exact count of work left.
    assert _bm25vector_literal([], []) == "{}"
    # A fractional weight here means a pre-baked value reached the wrong
    # branch; it must never round down to a term that is not there.
    # Reached through the store this is now unreachable — `upsert_one` refuses
    # non-integral weights first. It is kept because it pins this function's
    # own contract: `round(0.4)` is 0, and a term the document contains must
    # not vanish. The guard above can move; this choice should not move with it
    # by accident.
    assert _bm25vector_literal([7], [0.4]) == "{7:1}"
    # `zip` would truncate silently and index the document under half its
    # terms — a document indexed under a subset of what it says, with nothing
    # downstream able to notice. Refused rather than produced.
    with pytest.raises(ValueError, match="disagree"):
        _bm25vector_literal([10, 20], [1.0])
    with pytest.raises(ValueError, match="disagree"):
        _bm25vector_literal([10, 20], [])


async def test_the_ddl_creates_an_index_the_planner_can_use():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            kind = await conn.fetchval(
                """
                SELECT am.amname FROM pg_class c
                  JOIN pg_am am ON am.oid = c.relam
                 WHERE c.relname = 'idx_vi_chunks_bm25'
                """
            )
        assert kind == "bm25", (
            "DDL 이 bm25 인덱스를 안 만들었다 — 이 모양의 값어치가 통째로 사라진다"
        )


async def test_the_ddl_leaves_a_populated_tables_index_to_the_backfill():
    """Startup builds the BM25 index only for an empty table (akb#615).

    Over existing chunks the index is `scripts/backfill_bm25_vector.py
    --index`'s job: it builds CONCURRENTLY, and refuses while any row still has
    no vector. `_do_ensure` runs in one transaction, so building it there held
    a ShareLock against every write for the whole build, over whatever part of
    the column happened to be filled, and turned the rest of the sweep into the
    kind that measured 42x slower per batch. Selecting the shape before the
    runbook has run must fail visibly instead, and build nothing.
    """
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            chunk = uuid.uuid4()
            # A row written under `posting`: no vector yet.
            await conn.execute(
                """
                INSERT INTO vector_index.chunks
                    (chunk_id, source_type, source_id, vault_id,
                     section_path, content, chunk_index)
                VALUES ($1, 'document', $1, $2, '', 'indexed under posting', 0)
                """,
                chunk, uuid.uuid4(),
            )
            await conn.execute("DROP INDEX vector_index.idx_vi_chunks_bm25")

        restarted = PgvectorStore(
            dsn=store._dsn, schema="vector_index",
            dense_dim=4, sparse_shape="vchord",
        )
        try:
            with pytest.raises(VectorStoreUnavailable, match="--index"):
                await restarted.ensure_collection()
        finally:
            if restarted._own_pool is not None:
                await restarted._own_pool.close()

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT to_regclass('vector_index.idx_vi_chunks_bm25')"
            ) is None


async def test_a_written_chunk_comes_back_from_a_search():
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            for i, terms in enumerate([[10, 10, 20], [20, 30], [40]], start=1):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(vid),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None, sparse_indices=terms,
                    sparse_values=[1.0] * len(terms), conn=conn,
                )
            hits = await store._search_sparse(
                conn, terms=[10], weights=[1.0], filter_uuids=None,
                filter_col="vault_id", limit=5,
            )
        assert str(uuid.UUID(int=1)) in hits, "쓴 청크가 검색에 안 나온다"


# Past int4 and inside the u32 range the index stores. Term ids are `bigint`
# where they are minted; the query path bound them as `int4`.
_PAST_INT4 = 3_000_000_000
# The first id the index cannot hold: its text input takes u32.
_PAST_U32 = 4_294_967_296


@pytest.mark.parametrize("shape", ["unfiltered", "index-led", "materialised"])
async def test_a_term_id_past_int4_is_found_by_every_query_shape(shape):
    """Written through the text input, never found through `int4` (akb#665).

    Documents reach the index as a `{id:tf}` literal, which takes any id up to
    4,294,967,295. Queries were bound as `int[]`, and asyncpg refuses anything
    past 2,147,483,647 before the query is sent. The index-led case asks for
    more rows than its scope holds, so the global-candidate probe runs as well.
    """
    async with _store() as (store, pool):
        vault = uuid.uuid4()
        async with pool.acquire() as conn:
            for i, terms in enumerate([[20, _PAST_INT4], [20, 30]], start=1):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(vault),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None, sparse_indices=terms,
                    sparse_values=[1.0] * len(terms), conn=conn,
                )
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(type(store), "_filter_is_selective",
                           lambda *a, **k: _const(shape == "materialised"))
                hits = await store._search_sparse(
                    conn, terms=[_PAST_INT4], weights=[1.0],
                    filter_uuids=None if shape == "unfiltered" else [vault],
                    filter_col="vault_id", limit=5,
                )
        assert hits == [str(uuid.UUID(int=1))]


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_the_vector_literal_names_the_bound_the_index_holds():
    """One bound, stated where every vector is built (akb#665).

    The index's text input answers an id past u32, or a negative one, with
    "Bad parsing at position N". A document holding one is refused with the
    bound named, rather than indexed under a subset of its terms."""
    with pytest.raises(ValueError, match="0 to 4,294,967,295"):
        _bm25vector_literal([20, _PAST_U32], [1.0, 1.0])
    with pytest.raises(ValueError, match="0 to 4,294,967,295"):
        _bm25vector_literal([-1], [1.0])
    assert _bm25vector_literal([4_294_967_295], [1.0]) == "{4294967295:1}"


async def test_a_query_term_the_index_cannot_hold_matches_nothing():
    """No document can hold such a term, so it cannot fail the search (akb#665)."""
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            await store.upsert_one(
                chunk_id=str(uuid.UUID(int=1)), source_type="document",
                source_id=str(uuid.uuid4()), vault_id=str(uuid.uuid4()),
                section_path="", content="doc", chunk_index=1,
                dense=None, sparse_indices=[20], sparse_values=[1.0], conn=conn,
            )
            alone = await store._search_sparse(
                conn, terms=[_PAST_U32], weights=[1.0], filter_uuids=None,
                filter_col="vault_id", limit=5,
            )
            mixed = await store._search_sparse(
                conn, terms=[_PAST_U32, 20], weights=[1.0, 1.0], filter_uuids=None,
                filter_col="vault_id", limit=5,
            )
    assert alone == []
    assert mixed == [str(uuid.UUID(int=1))]


async def test_both_query_shapes_return_the_same_rows():
    """The selectivity branch chooses for speed. If it also changed the answer,
    the threshold would be a correctness knob rather than a performance one.

    The documents are built so the ranking is unambiguous: every one contains
    the query term exactly once and differs only in length, so BM25's length
    normalisation orders them strictly. The first version of this test cycled
    the term frequency through four values, which made most documents tie — and
    then the two shapes disagreed, because a tie has no right answer and each
    broke it differently. That is worth knowing, but it is not what this test
    is for, and asserting it here would have been asserting the tie-break."""
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            for i in range(1, 31):
                terms = [10] + [1000 + j for j in range(i)]
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(vid),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None, sparse_indices=terms,
                    sparse_values=[1.0] * len(terms), conn=conn,
                )
            await conn.execute("ANALYZE vector_index.chunks")

            async def hits(selective: bool) -> list[str]:
                original = store._filter_is_selective
                store._filter_is_selective = lambda *a, **k: _const(selective)  # type: ignore[assignment]
                try:
                    return await store._search_sparse(
                        conn, terms=[10], weights=[1.0], filter_uuids=[vid],
                        filter_col="vault_id", limit=10,
                    )
                finally:
                    store._filter_is_selective = original  # type: ignore[assignment]

            materialised = await hits(True)
            index_led = await hits(False)
        assert materialised == index_led, (
            f"두 모양이 다른 결과를 낸다\n  물질화: {materialised}\n  인덱스: {index_led}"
        )
        assert len(materialised) == 10
        # Shortest documents score highest for a term every document holds once.
        assert materialised == [str(uuid.UUID(int=i)) for i in range(1, 11)]


async def _const(value):
    return value


async def test_the_selectivity_estimate_agrees_with_the_truth():
    """A filter covering most rows is not selective; one covering a handful is.
    The estimate only has to be right about that, which is what is asserted."""
    async with _store() as (store, pool):
        big, small = uuid.uuid4(), uuid.uuid4()
        async with pool.acquire() as conn:
            for i in range(1, 301):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()),
                    vault_id=str(big if i > 2 else small),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None, sparse_indices=[10], sparse_values=[1.0], conn=conn,
                )
            await conn.execute("ANALYZE vector_index.chunks")
            assert await store._filter_is_selective(conn, "vault_id", [small]) is True
            assert await store._filter_is_selective(conn, "vault_id", [big]) is False


async def test_a_statistics_failure_does_not_take_the_search_with_it():
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            broken = PgvectorStore(
                dsn=store._dsn, schema="no_such_schema",
                dense_dim=4, sparse_shape="vchord",
            )
            assert await broken._filter_is_selective(conn, "vault_id", [uuid.uuid4()]) is False


async def test_a_selective_filter_does_not_let_the_bm25_index_lead():
    """The one thing the returned rows cannot show.

    Both shapes answer identically — that is asserted above and is the point of
    having a threshold at all. So removing the materialised branch changes
    nothing observable in the results, and a suite that only checks results
    calls that fine. It is not fine: at 0.19% selectivity the index-led shape
    measured 21x slower, and under a prepared statement its worst case was
    1,800x (akb#626).

    What separates them is which SQL went out; the plan itself is asserted by
    the test below, which catches what a text match cannot."""
    async with _store() as (store, pool):
        vid, bulk = uuid.uuid4(), uuid.uuid4()
        async with pool.acquire() as conn:
            for i in range(1, 401):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()),
                    vault_id=str(vid if i <= 2 else bulk),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None, sparse_indices=[10], sparse_values=[1.0], conn=conn,
                )
            await conn.execute("ANALYZE vector_index.chunks")
            assert await store._filter_is_selective(conn, "vault_id", [vid]) is True

            # asyncpg connections use __slots__, so the spy goes on the class.
            captured: list[str] = []
            real_fetch = type(conn).fetch

            async def spy(self, sql, *args, **kwargs):
                captured.append(sql)
                return await real_fetch(self, sql, *args, **kwargs)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(type(conn), "fetch", spy, raising=True)
                await store._search_sparse(
                    conn, terms=[10], weights=[1.0], filter_uuids=[vid],
                    filter_col="vault_id", limit=5,
                )

        search_sql = [q for q in captured if "sparse_bm25" in q]
        assert search_sql, "검색 SQL 을 못 잡았다"
        # "AS MATERIALIZED", not "MATERIALIZED": the mutation that undoes this
        # optimisation is `AS MATERIALIZED` -> `AS NOT MATERIALIZED`, and the
        # substring survives it.
        assert "AS MATERIALIZED" in search_sql[-1], (
            "선택적인 필터인데 인덱스가 앞장서는 모양이 나갔다 — 결과는 같지만 "
            "실측상 21배 느리고, prepared 상태에서 최악은 1,800배다"
        )


async def test_documents_without_the_query_term_never_fill_the_page():
    """`<&>` orders the whole table, it does not filter.

    A document holding none of the query terms scores exactly `-0`, which sorts
    after every match and before nothing — so a plain `ORDER BY ... LIMIT k`
    tops the page up with irrelevant chunks whenever fewer than k match. With
    `prefetch_per_leg` at max(limit*3, 50) that is up to fifty of them reaching
    RRF, tied at -0 and ordered by whatever the heap gave.

    The earlier version of the shape-agreement test could not see this: all
    thirty of its documents held the query term, so the page was never short.
    """
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            for i in range(1, 31):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(vid),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None,
                    sparse_indices=[10] if i <= 3 else [777],
                    sparse_values=[1.0], conn=conn,
                )
            await conn.execute("ANALYZE vector_index.chunks")
            matching = {str(uuid.UUID(int=i)) for i in (1, 2, 3)}

            for selective in (True, False):
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(
                        type(store), "_filter_is_selective",
                        lambda *a, s=selective, **k: _const(s),
                    )
                    hits = await store._search_sparse(
                        conn, terms=[10], weights=[1.0], filter_uuids=[vid],
                        filter_col="vault_id", limit=10,
                    )
                assert set(hits) == matching, (
                    f"selective={selective}: 질의어를 안 가진 청크가 섞였다 — {hits}"
                )

            unfiltered = await store._search_sparse(
                conn, terms=[10], weights=[1.0], filter_uuids=None,
                filter_col="vault_id", limit=10,
            )
        assert set(unfiltered) == matching


async def test_an_empty_vector_is_stored_and_never_displaces_a_match():
    """`'{}'` is written for a document with no terms, and costs nothing.

    The column has to distinguish "nothing encoded this row yet" (`NULL`) from
    "something did and there were no terms" (`'{}'`), because only then is
    `sparse_bm25 IS NULL` an exact count of what a backfill still owes. The
    objection to storing it was that `'{}'` passes the `IS NOT NULL` guard and
    scores `-0`, so it would top the page up the way the test above describes.

    It does not, and the reason is the order: `-0` sorts after every negative
    score, so an empty row can only take a slot no match wanted, and
    `-0 < 0` is false so the outer filter drops it there. This asserts it at a
    scale where a wrong answer could not hide — three matches against two
    hundred empty rows, asked for ten."""
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            for i in range(1, 4):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(vid),
                    section_path="", content=f"hit{i}", chunk_index=i,
                    dense=None, sparse_indices=[10], sparse_values=[float(i)],
                    conn=conn,
                )
            for i in range(100, 300):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(vid),
                    section_path="", content="", chunk_index=i,
                    dense=None, sparse_indices=[], sparse_values=[], conn=conn,
                )
            await conn.execute("ANALYZE vector_index.chunks")

            stored = await conn.fetch(
                "SELECT sparse_bm25::text AS v FROM vector_index.chunks "
                "WHERE chunk_index >= 100"
            )
            assert len(stored) == 200
            assert {r["v"] for r in stored} == {"{}"}, "빈 문서가 NULL 로 갔다"

            matching = {str(uuid.UUID(int=i)) for i in (1, 2, 3)}
            for selective in (True, False):
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(
                        type(store), "_filter_is_selective",
                        lambda *a, s=selective, **k: _const(s),
                    )
                    hits = await store._search_sparse(
                        conn, terms=[10], weights=[1.0], filter_uuids=[vid],
                        filter_col="vault_id", limit=10,
                    )
                assert set(hits) == matching, (
                    f"selective={selective}: 빈 벡터가 자리를 먹었다 — {hits}"
                )


async def test_the_shape_reaches_the_encoder_from_the_store_not_the_setting():
    """The bench harness builds one store per shape while `settings` never
    moves, and encodes its corpus once for all of them. Reading the setting
    would hand pre-baked weights to the shape that must not have them."""
    from app.services import sparse_encoder

    async with _store() as (store, _pool):
        assert store.sparse_shape == "vchord"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sparse_encoder.settings, "vector_store_driver", "pgvector")
            mp.setattr(sparse_encoder.settings, "vector_store_sparse_shape", "posting")
            assert sparse_encoder._use_raw_weights(store.sparse_shape) is True
            assert sparse_encoder._use_raw_weights() is False


async def test_pre_baked_weights_are_refused_rather_than_stored():
    """A caller that bypasses the plumbing gets an error, not a corpus indexed
    at the wrong scale — and the error says "this row is wrong", not "the
    database is down".

    `embed_worker` treats `VectorStoreUnavailable` as a transient outage: it
    fails the row, returns the rest of the batch unattempted and stops. A
    pre-baked weight is deterministic and no retry fixes it, so that
    classification would halt indexing and report the wrong cause. `ValueError`
    reaches the per-row failure path instead, which is where
    `_bm25vector_literal` already sends the same class of mistake."""
    async with _store() as (store, pool):
        async with pool.acquire() as conn:
            with pytest.raises(ValueError, match="non-integral"):
                await store.upsert_one(
                    chunk_id=str(uuid.uuid4()), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(uuid.uuid4()),
                    section_path="", content="x", chunk_index=0, dense=None,
                    sparse_indices=[10], sparse_values=[1.37], conn=conn,
                )


async def test_the_statistics_read_happens_outside_the_transaction():
    """Where the read sits is the whole of finding 3, and rows cannot show it.

    Inside the transaction, a server-side error aborts it; catching the Python
    exception does not un-abort it, so the next statement fails with
    InFailedSQLTransactionError — a PostgresError, which `hybrid_search` turns
    into VectorStoreUnavailable, taking the dense leg down with a search that
    should merely have fallen back to the index-led shape.

    Forcing the estimate to raise is what separates the two placements: outside,
    the search still runs; inside, the transaction is already dead."""
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            await store.upsert_one(
                chunk_id=str(uuid.UUID(int=1)), source_type="document",
                source_id=str(uuid.uuid4()), vault_id=str(vid),
                section_path="", content="x", chunk_index=0, dense=None,
                sparse_indices=[10], sparse_values=[1.0], conn=conn,
            )

            real_fetchrow = type(conn).fetchrow

            async def boom(self, sql, *args, **kwargs):
                if "pg_stats" in sql:
                    # The shape a real failure takes: the server raises, so the
                    # transaction it was issued in is finished either way.
                    await real_fetchrow(self, "SELECT 1/0")
                return await real_fetchrow(self, sql, *args, **kwargs)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(type(conn), "fetchrow", boom)
                hits = await store._search_sparse(
                    conn, terms=[10], weights=[1.0], filter_uuids=[vid],
                    filter_col="vault_id", limit=5,
                )
            assert hits == [str(uuid.UUID(int=1))], (
                "통계 조회가 실패했다고 검색까지 죽었다 — 조회가 트랜잭션 안에 있다"
            )
            assert await conn.fetchval("SELECT 1") == 1


async def test_a_statistics_failure_leaves_the_connection_usable():
    """The estimate is read before the transaction opens, because a server-side
    error inside one aborts it and catching the exception in Python does not
    un-abort it — the next statement would fail with
    InFailedSQLTransactionError, which `hybrid_search` turns into
    VectorStoreUnavailable and which takes the dense leg down too."""
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            await store.upsert_one(
                chunk_id=str(uuid.UUID(int=1)), source_type="document",
                source_id=str(uuid.uuid4()), vault_id=str(vid),
                section_path="", content="x", chunk_index=0, dense=None,
                sparse_indices=[10], sparse_values=[1.0], conn=conn,
            )
            await conn.execute("SET statement_timeout = '1ms'")
            try:
                # Whether the timeout lands on the estimate or on the search is
                # not what this asserts; either way the connection has to come
                # back usable.
                await store._search_sparse(
                    conn, terms=[10], weights=[1.0], filter_uuids=[vid],
                    filter_col="vault_id", limit=5,
                )
            except asyncpg.PostgresError:
                pass
            finally:
                await conn.execute("RESET statement_timeout")
            # 핵심: 연결이 abort 상태로 남지 않았다.
            assert await conn.fetchval("SELECT 1") == 1


async def test_the_two_shapes_really_do_produce_different_plans():
    """What the SQL text cannot show: that the barrier does its job.

    `AS MATERIALIZED` is the whole mechanism — a CTE's output carries no index,
    so the sort above it has no way to reach the BM25 index, and the index-led
    form has nothing stopping the planner from using it. Asserting the text
    proves which branch ran; asserting the plan proves the branch means what it
    is supposed to mean.

    Scale is not needed for this, which was worth checking rather than
    assuming: the planner picks the BM25 index for the index-led form at thirty
    rows, and never picks it through the CTE barrier at any size measured up to
    200k. (The *latency* difference does need scale — the index-led form only
    becomes the slow one somewhere between 50k and 200k rows — but that is a
    benchmark's job, not this test's.)"""
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            for i in range(1, 31):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(vid),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None, sparse_indices=[10], sparse_values=[1.0], conn=conn,
                )
            await conn.execute("ANALYZE vector_index.chunks")

            captured: list[str] = []
            real_fetch = type(conn).fetch

            async def spy(self, sql, *args, **kwargs):
                if "sparse_bm25" in sql:
                    captured.append(sql)
                return await real_fetch(self, sql, *args, **kwargs)

            plans = {}
            for selective in (True, False):
                captured.clear()
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(type(conn), "fetch", spy)
                    mp.setattr(
                        type(store), "_filter_is_selective",
                        lambda *a, s=selective, **k: _const(s),
                    )
                    await store._search_sparse(
                        conn, terms=[10], weights=[1.0], filter_uuids=[vid],
                        filter_col="vault_id", limit=5,
                    )
                sql = captured[-1]
                async with conn.transaction():
                    await conn.execute(
                        'SET LOCAL search_path TO "$user", public, bm25_catalog'
                    )
                    rows = await conn.fetch(
                        f"EXPLAIN {sql}", [10], [vid], 5,
                    )
                plans[selective] = "\n".join(r[0] for r in rows)

        index_node = "Index Scan using idx_vi_chunks_bm25"
        assert index_node in plans[False], (
            f"인덱스가 앞장서야 하는데 안 쓴다\n{plans[False]}"
        )
        assert index_node not in plans[True], (
            "선택적 필터인데 bm25 인덱스가 정렬을 이끈다 — MATERIALIZED 배리어가 "
            f"제 일을 못 하고 있다\n{plans[True]}"
        )
        assert "CTE candidate_chunks" in plans[True]


@pytest.mark.parametrize("selective", [True, False, None],
                         ids=["materialised", "index-led", "unfiltered"])
async def test_source_type_filters_in_every_branch(selective):
    """The branch that never ran.

    Each of the three shapes spells its own `$n`, and `source_type` is the last
    slot in each — `$4` where a filter is present, `$3` where it is not. No
    test passed `source_type_values` at all, so a swapped or missing number
    would have gone out green in two branches out of three.

    Both directions are exercised: a type that matches must keep the rows, and
    one that does not must remove them. Asserting only the first would pass for
    a predicate that was silently dropped."""
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            for i in range(1, 11):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)),
                    source_type="document" if i <= 6 else "table",
                    source_id=str(uuid.uuid4()), vault_id=str(vid),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None, sparse_indices=[10], sparse_values=[1.0], conn=conn,
                )
            await conn.execute("ANALYZE vector_index.chunks")

            async def hits(types):
                ctx = contextlib.nullcontext()
                if selective is not None:
                    ctx = pytest.MonkeyPatch.context()
                with ctx as mp:
                    if mp is not None:
                        mp.setattr(
                            type(store), "_filter_is_selective",
                            lambda *a, s=selective, **k: _const(s),
                        )
                    return await store._search_sparse(
                        conn, terms=[10], weights=[1.0],
                        filter_uuids=None if selective is None else [vid],
                        filter_col="vault_id", source_type_values=types, limit=20,
                    )

            assert len(await hits(None)) == 10
            assert len(await hits(["document"])) == 6, "일치하는 source_type 이 걸러졌다"
            assert len(await hits(["table"])) == 4
            assert await hits(["memory"]) == [], "없는 source_type 이 안 걸러졌다"


async def test_a_term_in_almost_every_document_is_not_filtered_away():
    """The other direction of `WHERE score < 0`.

    The page-padding test asserts that non-matching documents stay out. The
    filter's own risk is the reverse: if a matching document could score zero
    or above it would be discarded, and that happens for classic BM25 IDF once
    a term is in more than half the corpus — silently, and only for the terms
    most queries contain.

    This extension's log1p IDF does not go negative, which is why the filter is
    safe. That is a property of the pinned extension rather than of this code,
    so `deploy/postgres/README.md` carries it on the pin-bump checklist — but a
    checklist only works if somebody reads it."""
    async with _store() as (store, pool):
        vid = uuid.uuid4()
        async with pool.acquire() as conn:
            for i in range(1, 51):
                await store.upsert_one(
                    chunk_id=str(uuid.UUID(int=i)), source_type="document",
                    source_id=str(uuid.uuid4()), vault_id=str(vid),
                    section_path="", content=f"doc{i}", chunk_index=i,
                    dense=None, sparse_indices=[10], sparse_values=[1.0], conn=conn,
                )
            await conn.execute("ANALYZE vector_index.chunks")
            hits = await store._search_sparse(
                conn, terms=[10], weights=[1.0], filter_uuids=[vid],
                filter_col="vault_id", limit=50,
            )
        assert len(hits) == 50, (
            f"모든 문서가 가진 용어인데 {50 - len(hits)}건이 잘려나갔다 — "
            f"확장의 IDF 가 고-df 에서 음수가 아니게 됐을 수 있다"
        )


@pytest.mark.parametrize("scale", [1, 10], ids=["small", "large"])
async def test_the_selectivity_estimate_works_outside_the_common_values(scale):
    """The branch production actually takes — at both scales, because they are
    not the same branch.

    `pg_stats` records only the hundred most common values. The other estimate
    test uses two vaults, so both sit in that list and the `remainder / others`
    arm never runs — yet that is the arm a median vault falls into, being far
    outside the top hundred.

    Two scales because PostgreSQL records `n_distinct` differently at each: at
    roughly two thousand rows it stores a NEGATIVE value, a ratio of the row
    count, which takes the `abs(n_distinct) * total` path; at twenty thousand
    it stores a positive count and takes the other. Running one scale leaves
    the other arm unguarded.

    The distribution has to be skewed. Spread evenly, every vault is equally
    selective and the estimate is trivially right — there is no "outside the
    common values AND not selective" case to get wrong.

    Rows go in with `executemany` rather than `upsert_one`: this test needs a
    `vault_id` distribution, not vectors, and the driver loop costs 45x as much
    (32.8s against 0.73s) for nothing it asserts."""
    async with _store() as (store, pool):
        head, second = uuid.uuid4(), uuid.uuid4()
        tails = [uuid.uuid4() for _ in range(398)]
        rows = (
            [(uuid.uuid4(), head)] * (400 * scale)
            + [(uuid.uuid4(), second)] * (200 * scale)
            + [(uuid.uuid4(), t) for t in tails for _ in range(4 * scale)]
        )
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO vector_index.chunks
                    (chunk_id, source_type, source_id, vault_id,
                     section_path, content, chunk_index)
                VALUES ($1, 'document', $1, $2, '', 'x', 0)
                """,
                [(uuid.uuid4(), vault) for _, vault in rows],
            )
            await conn.execute("ANALYZE vector_index.chunks")

            n_distinct = await conn.fetchval(
                """
                SELECT n_distinct FROM pg_stats
                 WHERE schemaname='vector_index' AND tablename='chunks'
                   AND attname='vault_id'
                """
            )
            mcv = set(await conn.fetchval(
                """
                SELECT most_common_vals::text::uuid[] FROM pg_stats
                 WHERE schemaname='vector_index' AND tablename='chunks'
                   AND attname='vault_id'
                """
            ) or [])
            # ANALYZE samples, so WHICH tails reach the common-value list varies
            # run to run; naming one and asserting it is absent fails about one
            # run in four. That at least one of 398 misses 100 slots does not.
            outside = [t for t in tails if t not in mcv]
            assert outside, "tail vault 가 전부 MCV 안에 들어갔다 — 목표한 가지를 안 탄다"
            assert head in mcv, "head 가 MCV 밖이면 이 구성이 치우치지 않았다"

            assert await store._filter_is_selective(conn, "vault_id", [head]) is False
            assert await store._filter_is_selective(conn, "vault_id", [second]) is False
            assert await store._filter_is_selective(conn, "vault_id", [outside[0]]) is True
        # Recorded so a change in how PostgreSQL stores this is visible here
        # rather than as a silent loss of coverage on one of the two arms.
        assert (n_distinct < 0) is (scale == 1), (
            f"scale={scale} 에서 n_distinct={n_distinct} — 두 규모가 같은 가지를 "
            f"타고 있다면 하나는 값어치가 없다"
        )
