"""The `sparse_bm25` backfill, against a PostgreSQL carrying the extension.

A migration tool that was never run is not evidence, and the parts worth
asserting are the ones the tool itself contributes: that `IS NULL` really is the
queue and empties, that the forward sweep does not stall or skip, and that
neither pass overwrites something the running indexer wrote underneath it. The
encoder is exercised for real (`get_or_create_term_ids` against a live vocab
table) with only Kiwi replaced, because a word split is deterministic and this
file is not where tokenization is decided.

Needs `AKB_VCHORD_TEST_DSN`; absent that these skip, and say so.
"""
from __future__ import annotations

import contextlib
import os
import uuid

import asyncpg
import pytest

from app.db.postgres import _load_migration
from app.services import sparse_encoder
from app.services.vector_store.pgvector import PgvectorStore

from scripts.backfill_bm25_vector import _build_index, _counts, _pass, _prepare

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_VCHORD_TEST_DSN", "")
_SCHEMA = "vector_index"


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


async def _vocab_ddl(conn) -> None:
    """The slice of migration 005 the encoder needs — nothing else of it."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bm25_vocab (
            term TEXT PRIMARY KEY,
            term_id BIGINT NOT NULL UNIQUE,
            df BIGINT NOT NULL DEFAULT 0,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute("CREATE SEQUENCE IF NOT EXISTS bm25_term_id_seq AS BIGINT START 1")
    # The encoder reports, and the writers check, the numbering its ids are in
    # (akb#687) — migration 113 itself rather than a copy of it.
    await _load_migration("113_bm25_vocab_epoch.py").migrate(conn)
    # The pre-flip write path saturates against these, so they have to be
    # readable — the values do not matter here, only that `posting` encoding
    # produces something and `vchord` encoding ignores them.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bm25_stats (
            id INTEGER PRIMARY KEY DEFAULT 1,
            total_docs BIGINT NOT NULL DEFAULT 0,
            avgdl DOUBLE PRECISION NOT NULL DEFAULT 0,
            tokenizer_name TEXT NOT NULL DEFAULT 'kiwi',
            tokenizer_version TEXT NOT NULL DEFAULT '0',
            k1 DOUBLE PRECISION NOT NULL DEFAULT 1.5,
            b DOUBLE PRECISION NOT NULL DEFAULT 0.75,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (id = 1)
        )
        """
    )
    await conn.execute(
        "INSERT INTO bm25_stats (id, total_docs, avgdl) VALUES (1, 100, 3) "
        "ON CONFLICT (id) DO NOTHING"
    )


@contextlib.asynccontextmanager
async def _corpus(monkeypatch, *, shape: str = "posting"):
    """A database holding a corpus written the OLD way, plus a live vocab.

    The store is built with `shape` so the rows land exactly as a deployment
    that has not flipped yet would have them: no `sparse_bm25` column at all.
    """
    if not _DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN 미설정 — 확장이 있는 PostgreSQL 이 필요하다")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_bfill_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=8)
    try:
        async with pool.acquire() as conn:
            await _vocab_ddl(conn)

        # The encoder's vocab lives in the main pool; here both are this
        # database, which is what lets the backfill run end to end.
        async def _main_pool():
            return pool

        monkeypatch.setattr(sparse_encoder, "get_pool", _main_pool)

        async def _split(text: str) -> list[str]:
            return [w for w in text.lower().split() if w]

        monkeypatch.setattr(sparse_encoder, "tokenize", _split)

        store = PgvectorStore(
            dsn=_database_dsn(name), schema=_SCHEMA, dense_dim=4, sparse_shape=shape,
        )
        async with pool.acquire() as conn:
            await store._do_ensure(conn)
        yield store, pool
    finally:
        await pool.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _no_sleep(_seconds):
    """Retries are asserted by count, not by wall clock."""


async def _write_old(store, pool, chunk_id: uuid.UUID, content: str, idx: int) -> None:
    """One chunk through the pre-flip write path (`posting` shape)."""
    terms, weights = await sparse_encoder.encode_document(content)
    async with pool.acquire() as conn:
        await store.upsert_one(
            chunk_id=str(chunk_id), source_type="document",
            source_id=str(uuid.uuid4()), vault_id=str(uuid.UUID(int=1)),
            section_path="", content=content, chunk_index=idx, dense=None,
            sparse_indices=terms, sparse_values=weights, conn=conn,
        )


async def _bm25_indexes(pool):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT c.relname FROM pg_index i "
            "  JOIN pg_class c ON c.oid = i.indexrelid "
            "  JOIN pg_class t ON t.oid = i.indrelid "
            "  JOIN pg_namespace n ON n.oid = t.relnamespace "
            "  JOIN pg_am am ON am.oid = c.relam "
            " WHERE t.relname='chunks' AND n.nspname=$1 AND am.amname='bm25'",
            _SCHEMA,
        )
    return {r["relname"] for r in rows}


async def _column_count(pool):
    async with pool.acquire() as c:
        return int(await c.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='chunks' "
            "AND column_name='sparse_bm25'", _SCHEMA,
        ))


async def test_prepare_adds_the_column_and_deliberately_not_the_index(monkeypatch):
    """The order is the whole performance story, so it is pinned here.

    Measured on a 2.1M-chunk corpus: one 500-row batch took 103 seconds with the
    index present and 2.5 without. Every row written into an existing BM25 index
    pays its per-term maintenance, and paying that two million times instead of
    once is the difference between hours and days. A `--prepare` that helpfully
    created the index would put the sweep back on the slow path with nothing
    failing to say so — which is exactly what happened before this assertion
    existed."""
    async with _corpus(monkeypatch) as (_store, pool):
        assert await _column_count(pool) == 0

        await _prepare(pool, _SCHEMA)
        await _prepare(pool, _SCHEMA)  # idempotent

        assert await _column_count(pool) == 1
        assert await _bm25_indexes(pool) == set(), (
            "--prepare 가 인덱스를 만들었다 — sweep 이 행마다 인덱스 유지비를 낸다"
        )


async def test_the_index_build_refuses_a_half_filled_column(monkeypatch):
    """Building early is not wrong, it is slow — and slow in a way that looks
    like nothing. Refusing names the remaining work instead."""
    async with _corpus(monkeypatch) as (store, pool):
        await _write_old(store, pool, uuid.uuid4(), "one chunk", 0)
        await _prepare(pool, _SCHEMA)

        with pytest.raises(SystemExit, match="still have no vector"):
            await _build_index(pool, _SCHEMA)
        assert await _bm25_indexes(pool) == set()

        await _pass(pool, _SCHEMA, None)
        await _build_index(pool, _SCHEMA)
        assert len(await _bm25_indexes(pool)) == 1


async def test_the_index_build_uses_the_name_the_store_will_look_for(monkeypatch):
    """The name is written down in two places and they have to agree.

    `_do_ensure` creates `idx_vi_chunks_bm25 IF NOT EXISTS` on boot and
    `_search_sparse` names it inside `to_bm25query`. If this script built it
    under a different name the divergence would not error: the store's
    `IF NOT EXISTS` would find nothing and build its own — inside the
    transaction this script exists to stay out of, holding a ShareLock against
    every INSERT for the length of a full index build.

    So this asserts against the store rather than against a literal: run
    `_do_ensure` for the `vchord` shape over an already-prepared table and
    require that it adds no index.
    """
    async with _corpus(monkeypatch) as (store, pool):
        await _write_old(store, pool, uuid.uuid4(), "anything at all", 0)
        await _prepare(pool, _SCHEMA)
        await _pass(pool, _SCHEMA, None)
        await _build_index(pool, _SCHEMA)

        prepared = await _bm25_indexes(pool)
        assert len(prepared) == 1

        after_store = PgvectorStore(
            dsn=store._dsn, schema=_SCHEMA, dense_dim=4, sparse_shape="vchord",
        )
        async with pool.acquire() as conn:
            await after_store._do_ensure(conn)

        assert await _bm25_indexes(pool) == prepared, (
            "스토어가 다른 이름으로 인덱스를 하나 더 지었다 — 전환 후 첫 부팅이 "
            "트랜잭션 안에서 전체 빌드를 한다"
        )


async def test_the_sweep_empties_the_queue_and_an_empty_document_does_not_stall_it(
    monkeypatch,
):
    """`IS NULL` is the queue, so it has to reach zero — including for a chunk
    whose content yields no terms. If such a row stayed NULL the sweep would
    still terminate (it walks forward), but `--check` would never read 0 and an
    operator would have no completion signal at all."""
    async with _corpus(monkeypatch) as (store, pool):
        ids = [uuid.UUID(int=i) for i in range(1, 12)]
        for n, cid in enumerate(ids):
            # The last one is deliberately empty.
            await _write_old(store, pool, cid, "" if n == 10 else f"alpha beta{n}", n)
        await _prepare(pool, _SCHEMA)

        nulls, _ = await _counts(pool, _SCHEMA, None)
        assert nulls == len(ids)

        written = await _pass(pool, _SCHEMA, None)
        assert written == len(ids)

        nulls, _ = await _counts(pool, _SCHEMA, None)
        assert nulls == 0, "빈 문서가 큐에 남았다"

        async with pool.acquire() as c:
            empty = await c.fetchval(
                f'SELECT sparse_bm25::text FROM "{_SCHEMA}".chunks '
                "WHERE chunk_index = 10"
            )
        assert empty == "{}"

        # A second sweep has nothing to do — idempotent, not merely repeatable.
        assert await _pass(pool, _SCHEMA, None) == 0


@pytest.mark.parametrize("writers", [1, 3, 7], ids=["one", "three", "seven"])
async def test_splitting_the_write_changes_nothing_but_the_wall_clock(
    monkeypatch, writers
):
    """The batch is cut into `writers` parts that run at once, because the write
    is disk-latency bound on a table carrying a large HNSW index — one writer
    measured 4-16 rows/s against 45-62 for four. Concurrency added for speed has
    to be provably invisible in the result, so the same corpus is swept at three
    widths, including ones that do not divide the batch evenly, and the vectors
    are compared against what a single writer produces.
    """
    from scripts import backfill_bm25_vector as bf

    monkeypatch.setattr(bf, "_BATCH", 5)
    async with _corpus(monkeypatch) as (store, pool):
        ids = [uuid.UUID(int=i) for i in range(1, 18)]
        for n, cid in enumerate(ids):
            await _write_old(store, pool, cid, f"alpha beta{n} gamma{n % 3}", n)
        await _prepare(pool, _SCHEMA)

        # The flag has to reach the database as that many statements. Equivalence
        # alone cannot see this — a `writers` that was silently ignored would
        # pass every assertion below while the speed it exists for never
        # arrived, and nothing would say so.
        real_apply = bf._apply
        pending = {"n": 0}

        async def counting_apply(*a, **k):
            pending["n"] += 1
            return await real_apply(*a, **k)

        monkeypatch.setattr(bf, "_apply", counting_apply)

        assert await bf._pass(pool, _SCHEMA, None, writers) == len(ids)
        # 17 rows at a batch of 5 is 4 batches: 5,5,5,2. Each is cut into at
        # most `writers` parts, and a batch smaller than `writers` yields one
        # part per row rather than empty ones.
        expected = sum(min(writers, n) for n in (5, 5, 5, 2))
        assert pending["n"] == expected, (
            f"writers={writers}: _apply 가 {pending['n']}번 — {expected}번이어야 한다"
        )
        monkeypatch.setattr(bf, "_apply", real_apply)

        nulls, _ = await _counts(pool, _SCHEMA, None)
        assert nulls == 0

        async with pool.acquire() as c:
            got = {
                r["chunk_id"]: r["v"] for r in await c.fetch(
                    f'SELECT chunk_id, sparse_bm25::text AS v FROM "{_SCHEMA}".chunks'
                )
            }
        # Re-encode every chunk single-threaded and require the same vectors.
        async with pool.acquire() as c:
            await c.execute(f'UPDATE "{_SCHEMA}".chunks SET sparse_bm25 = NULL')
        assert await bf._pass(pool, _SCHEMA, None, 1) == len(ids)
        async with pool.acquire() as c:
            serial = {
                r["chunk_id"]: r["v"] for r in await c.fetch(
                    f'SELECT chunk_id, sparse_bm25::text AS v FROM "{_SCHEMA}".chunks'
                )
            }
        assert got == serial, f"writers={writers} 가 다른 벡터를 썼다"


async def test_a_deadlock_is_retried_rather_than_ending_the_sweep(monkeypatch):
    """Concurrency made this reachable, so the sweep has to survive it.

    Every writer's encoding upserts into `bm25_vocab`, and so does the stats
    recompute and the indexer. Live, at four writers with sixteen encodes each,
    a four-process cycle formed and the sweep died after 4% of the corpus —
    having looked alive the whole time, because the liveness check matched its
    own command line.

    Postgres picks a victim and aborts it; the work is idempotent and the next
    attempt meets a committed transaction instead of a live one. What is
    asserted here is that the retry happens and that the batch still lands.
    """
    from scripts import backfill_bm25_vector as bf

    async with _corpus(monkeypatch) as (store, pool):
        ids = [uuid.UUID(int=i) for i in range(1, 7)]
        for n, cid in enumerate(ids):
            await _write_old(store, pool, cid, f"term{n} shared", n)
        await _prepare(pool, _SCHEMA)

        real_once = bf._apply_once
        calls: list[int] = []

        async def flaky(pool_, schema, rows):
            calls.append(1)
            if len(calls) <= 2:
                raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
            return await real_once(pool_, schema, rows)

        monkeypatch.setattr(bf, "_apply_once", flaky)
        monkeypatch.setattr(bf.asyncio, "sleep", _no_sleep)

        assert await bf._pass(pool, _SCHEMA, None, 1) == len(ids)
        assert len(calls) == 3, f"재시도가 {len(calls)-1}번 — 2번이어야 한다"

        nulls, _ = await _counts(pool, _SCHEMA, None)
        assert nulls == 0


async def test_a_deadlock_that_never_clears_is_reported_not_swallowed(monkeypatch):
    """Retrying forever would turn a real problem into a silent stall — which
    is the shape this whole episode already took once."""
    from scripts import backfill_bm25_vector as bf

    async with _corpus(monkeypatch) as (store, pool):
        await _write_old(store, pool, uuid.uuid4(), "anything", 0)
        await _prepare(pool, _SCHEMA)

        tries: list[int] = []

        async def always(pool_, schema, rows):
            tries.append(1)
            raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")

        monkeypatch.setattr(bf, "_apply_once", always)
        monkeypatch.setattr(bf.asyncio, "sleep", _no_sleep)

        with pytest.raises(asyncpg.exceptions.DeadlockDetectedError):
            await bf._pass(pool, _SCHEMA, None, 1)
        assert len(tries) == bf._DEADLOCK_RETRIES


async def test_the_sweep_walks_past_a_batch_boundary(monkeypatch):
    """The forward cursor is the thing that could silently truncate: a sweep
    that restarted at the first unfilled row each time would still finish, and
    one that failed to advance would fill only the first batch and report a
    clean number. This corpus is larger than `_BATCH` so neither passes."""
    from scripts import backfill_bm25_vector as bf

    monkeypatch.setattr(bf, "_BATCH", 7)
    async with _corpus(monkeypatch) as (store, pool):
        n = 23
        for i in range(n):
            await _write_old(store, pool, uuid.uuid4(), f"term{i} shared", i)
        await _prepare(pool, _SCHEMA)

        assert await bf._pass(pool, _SCHEMA, None) == n
        nulls, _ = await _counts(pool, _SCHEMA, None)
        assert nulls == 0


@pytest.mark.parametrize("windowed", [False, True], ids=["null-pass", "since-pass"])
async def test_a_chunk_rewritten_between_read_and_write_is_left_alone(
    monkeypatch, windowed
):
    """The indexer keeps running, and the dangerous moment is inside a batch.

    A chunk re-indexed after the sweep read it holds NEW content; writing the
    encoding made from the old text would index it under words it no longer
    has, and nothing downstream could tell. The write is therefore conditioned
    on `indexed_at` still being the one that was read — which is what every
    write through the store moves.

    Both entry points are exercised, because the row reaches the sweep for a
    different reason in each: as a NULL in the bulk pass, and as a member of
    the window in a convergence pass.
    """
    from scripts import backfill_bm25_vector as bf

    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=7)
        await _write_old(store, pool, cid, "words as read", 0)
        await _prepare(pool, _SCHEMA)

        since = None
        if windowed:
            await _pass(pool, _SCHEMA, None)
            async with pool.acquire() as c:
                since = await c.fetchval("SELECT now()")
            await _write_old(store, pool, cid, "words as rewritten once", 1)

        async with pool.acquire() as c:
            before = await c.fetchval(
                f'SELECT sparse_bm25::text FROM "{_SCHEMA}".chunks WHERE chunk_id=$1',
                cid,
            )

        real_apply = bf._apply
        raced: list[int] = []

        async def racing_apply(pool_, schema, rows):
            if not raced:
                raced.append(1)
                # The real writer: content and indexed_at move together.
                await _write_old(store, pool_, cid, "words as rewritten again", 2)
            return await real_apply(pool_, schema, rows)

        monkeypatch.setattr(bf, "_apply", racing_apply)
        assert await bf._pass(pool, _SCHEMA, since) == 0, "경합한 행을 덮어썼다"
        assert raced == [1], "경합을 못 만들었다 — 테스트가 아무것도 주장하지 않는다"

        async with pool.acquire() as c:
            after = await c.fetchval(
                f'SELECT sparse_bm25::text FROM "{_SCHEMA}".chunks WHERE chunk_id=$1',
                cid,
            )
        assert after == before, "sweep 이 낡은 인코딩을 새 내용 위에 썼다"

        # Still owed: a window opened before that write finds it.
        _, owed = await _counts(pool, _SCHEMA, since)
        assert owed >= 1


async def test_a_rewrite_during_the_window_is_caught_by_since(monkeypatch):
    """A chunk edited after the sweep passed it keeps a vector describing the
    OLD content, and no `IS NULL` will ever find it. `--since` is the only thing
    that does."""
    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=3)
        await _write_old(store, pool, cid, "before", 0)
        await _prepare(pool, _SCHEMA)
        await _pass(pool, _SCHEMA, None)

        async with pool.acquire() as c:
            mark = await c.fetchval("SELECT now()")
            before = await c.fetchval(
                f'SELECT sparse_bm25::text FROM "{_SCHEMA}".chunks WHERE chunk_id=$1',
                cid,
            )

        # The pre-flip write path: content and indexed_at move, sparse_bm25 does not.
        await _write_old(store, pool, cid, "after entirely different", 1)

        async with pool.acquire() as c:
            still = await c.fetchval(
                f'SELECT sparse_bm25::text FROM "{_SCHEMA}".chunks WHERE chunk_id=$1',
                cid,
            )
        assert still == before, "전제가 깨졌다 — posting 경로가 sparse_bm25 를 건드렸다"

        nulls, stale = await _counts(pool, _SCHEMA, mark)
        assert (nulls, stale) == (0, 1)

        assert await _pass(pool, _SCHEMA, mark) == 1
        async with pool.acquire() as c:
            fixed = await c.fetchval(
                f'SELECT sparse_bm25::text FROM "{_SCHEMA}".chunks WHERE chunk_id=$1',
                cid,
            )
        assert fixed != before, "--since 가 낡은 벡터를 그대로 뒀다"


async def test_the_since_pass_leaves_a_row_rewritten_under_it_alone(monkeypatch):
    """Optimistic on `indexed_at`: if the chunk moved again between the read and
    the write, the encoding in hand is already stale and writing it would be the
    very thing this pass exists to undo. Skipping is correct — the next pass
    covers it, because its window starts before this one's write."""
    from scripts import backfill_bm25_vector as bf

    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=5)
        await _write_old(store, pool, cid, "first", 0)
        await _prepare(pool, _SCHEMA)
        await _pass(pool, _SCHEMA, None)

        async with pool.acquire() as c:
            mark = await c.fetchval("SELECT now()")
        await _write_old(store, pool, cid, "second version", 1)

        # Move indexed_at again between the sweep's read and its write.
        real_apply = bf._apply
        seen: list[int] = []

        async def racing_apply(pool_, schema, rows):
            if not seen:
                seen.append(1)
                async with pool_.acquire() as c:
                    await c.execute(
                        f'UPDATE "{schema}".chunks SET indexed_at = now() '
                        "WHERE chunk_id = $1", cid,
                    )
            return await real_apply(pool_, schema, rows)

        monkeypatch.setattr(bf, "_apply", racing_apply)
        assert await bf._pass(pool, _SCHEMA, mark) == 0, "경합한 행을 덮어썼다"

        # And it is still owed — a later window finds it.
        _, stale = await _counts(pool, _SCHEMA, mark)
        assert stale == 1


async def test_a_window_pass_also_takes_rows_that_appeared_during_it(monkeypatch):
    """A convergence pass has two kinds of debt, and reading for only one leaves
    the other with nothing that will ever find it.

    A chunk INSERTED while the previous pass ran is NULL — no `indexed_at`
    window is needed to see it, but a pass that reads only the window will not.
    A chunk REWRITTEN while it ran carries a vector and no NULL at all. Both
    have to be in the same question, or the operator converges one to zero
    while the other quietly stays."""
    async with _corpus(monkeypatch) as (store, pool):
        old = uuid.UUID(int=1)
        await _write_old(store, pool, old, "already here", 0)
        await _prepare(pool, _SCHEMA)
        await _pass(pool, _SCHEMA, None)

        async with pool.acquire() as c:
            mark = await c.fetchval("SELECT now()")

        fresh = uuid.UUID(int=2)
        await _write_old(store, pool, fresh, "arrived mid pass", 1)
        await _write_old(store, pool, old, "changed mid pass", 0)

        # And the case the window alone cannot see: a row the bulk pass never
        # reached, so it is NULL and its `indexed_at` is OLDER than the mark.
        # A crashed or interrupted bulk run leaves exactly these, and an
        # operator who then converges with --since would be told zero.
        unfinished = uuid.UUID(int=3)
        await _write_old(store, pool, unfinished, "bulk never got here", 2)
        async with pool.acquire() as c:
            await c.execute(
                f'UPDATE "{_SCHEMA}".chunks SET indexed_at = $1 WHERE chunk_id = $2',
                mark, unfinished,
            )

        nulls, owed = await _counts(pool, _SCHEMA, mark)
        assert nulls == 2, "새 행과 미완 행이 둘 다 NULL 이어야 한다"
        assert owed == 3, "창 패스는 새 행·수정된 행·미완 행을 전부 봐야 한다"

        assert await _pass(pool, _SCHEMA, mark) == 3
        nulls, _ = await _counts(pool, _SCHEMA, mark)
        assert nulls == 0


async def test_the_backfilled_corpus_answers_a_search(monkeypatch):
    """The point of all of it. Rows written before the flip must be findable
    through the index afterwards, and only the ones that hold the term."""
    async with _corpus(monkeypatch) as (store, pool):
        hits = [uuid.UUID(int=i) for i in (1, 2)]
        misses = [uuid.UUID(int=i) for i in (3, 4, 5)]
        for i, cid in enumerate(hits):
            await _write_old(store, pool, cid, f"needle haystack{i}", i)
        for i, cid in enumerate(misses):
            await _write_old(store, pool, cid, f"haystack{i} straw", 10 + i)
        await _prepare(pool, _SCHEMA)
        await _pass(pool, _SCHEMA, None)
        await _build_index(pool, _SCHEMA)

        term_ids = await sparse_encoder.get_or_create_term_ids(["needle"])
        needle = term_ids["needle"]

        after = PgvectorStore(
            dsn=store._dsn, schema=_SCHEMA, dense_dim=4, sparse_shape="vchord",
        )
        async with pool.acquire() as conn:
            await conn.execute(f'ANALYZE "{_SCHEMA}".chunks')
            found = await after._search_sparse(
                conn, terms=[needle], weights=[1.0], filter_uuids=None,
                filter_col="vault_id", limit=10,
            )
        assert set(found) == {str(c) for c in hits}
