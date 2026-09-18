#!/usr/bin/env python3
"""Backfill `sparse_bm25` onto existing pgvector points (akb#615).

The `vchord` sparse shape stores each chunk's terms in a `bm25vector` column and
lets a BM25 index do the scoring, instead of the `posting` table the backend
maintains itself. A corpus indexed under `posting` has that column empty, and a
row with `sparse_bm25 IS NULL` is excluded from sparse search — so flipping
`vector_store_sparse_shape` on a populated database would make every existing
chunk invisible to the sparse leg until something filled it in. This fills it
in, ahead of the flip, while `posting` is still serving.

    python -m scripts.backfill_bm25_vector --check
    python -m scripts.backfill_bm25_vector --prepare
    python -m scripts.backfill_bm25_vector
    python -m scripts.backfill_bm25_vector --since '2026-09-18T12:00:00+00:00'
    python -m scripts.backfill_bm25_vector --index

WHY IT NEEDS NO CURSOR AND NO DUAL WRITE
----------------------------------------
`NULL` means "nothing has encoded this row", and the encoder writes `'{}'` for a
document with no content-bearing terms rather than leaving it NULL — so
`sparse_bm25 IS NULL` *is* the queue, and an interrupted run resumes by asking
the same question again. There is no progress table to keep consistent, and
nothing to clean up.

The window is covered without teaching the write path to write both columns.
The `posting` branch of `upsert_one` stamps `indexed_at = NOW()` on every write
and does not touch `sparse_bm25`, which is what makes the two things that can
happen during a pass both visible afterwards:

    chunk inserted   sparse_bm25 NULL    IS NULL finds it
    chunk rewritten  sparse_bm25 STALE   indexed_at finds it
    chunk deleted    row gone            nothing to do

A `--since` pass looks for BOTH, so convergence is one question rather than two
that could each be answered while the other drifted. Each run prints the instant
to hand the next one, and the run that writes 0 is the one that found nothing
left:

    --prepare                      the column, milliseconds
    (no flag)                      the bulk of it, hours
    --since <that run's instant>   minutes
    --since <that run's instant>   until it writes 0
    --index                        once, over a full column
    flip vector_store_sparse_shape to vchord
    --since <the flip's instant>   once

After the flip the store writes the column itself, so anything touched later is
correct without help.

CONCURRENCY
-----------
The indexer keeps running throughout, and the dangerous moment is between a
row's read and its write. The rule is about content, not about NULL: an
encoding is written only while the row still holds the text it was made from,
which `indexed_at` identifies because every write through the store stamps it.
A chunk the indexer rewrote in that gap keeps what the store gave it and stays
owed to the next pass, instead of being stamped with tokens from text it no
longer has.

COST
----
Tokenizing looks like the whole cost and is not. Measured on a 2.1M-chunk
corpus, a 500-row batch spent 4.1 seconds encoding (123 chunks/s, the same rate
the stats recompute gets) and 21.8 seconds writing.

The write is expensive for a reason that has nothing to do with BM25. Adding a
column value to `chunks` is a non-HOT update — the table sits at the default
fillfactor 100, its rows average 1.1 KB, and only 2% of its tuples are dead, so
there is nowhere on the page for the new version (measured: 2.5% of updates were
HOT). A non-HOT update inserts into every index on the table, and one of them is
a 15 GB HNSW that does not fit in `shared_buffers`. That is 938 buffer accesses
per row, a third of them real reads, and it is disk latency rather than CPU.

Two things follow, and both are in this script rather than in advice:

- **The BM25 index is built last.** With it present a 500-row batch took 103
  seconds instead of 2.5 — every row would pay its per-term maintenance, and
  that index's cost is dominated by vocabulary breadth. `--prepare` adds only
  the column; `--index` builds it CONCURRENTLY at the end, over a column that
  is already full, so the build does not hold a ShareLock against every INSERT.
- **The writes are split.** Overlapping the disk waits is the only lever that
  works here: one writer measured 4-16 rows/s, two 32.3 and 32.5 (two
  orderings, 0.6% apart), four 45-62. `--writers` defaults to four.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg

from app.config import settings
from app.db.postgres import close_pool, init_db
from app.services import sparse_encoder
from app.services.vector_store import get_vector_store
from app.services.vector_store.pgvector import _bm25vector_literal, PgvectorStore

# Rows read per round trip. The write is one statement over the batch; the
# reads that matter are the per-chunk `get_or_create_term_ids` calls, which is
# why the batch is encoded with gather() rather than in sequence.
_BATCH = 500

# How many chunks are encoded at once. Kiwi runs in a process pool, so this
# bounds both the tokenizer queue and the number of vocab round trips in flight.
_CONCURRENCY = 16

# How many UPDATE statements run at once, and how long one is given.
#
# The write, not the tokenizing, is what this job costs. On a table carrying a
# 15 GB HNSW index over 2.1M rows, adding a column value is a non-HOT update
# (measured: 2.5% of updates were HOT — the table is at the default fillfactor
# 100 and only 2% of its tuples are dead), so every row also inserts into every
# index, the HNSW included. That insert walks a graph that does not fit in
# `shared_buffers`: 938 buffer accesses per row, a third of them real reads.
#
# It is therefore disk-latency bound, not CPU bound, and overlapping the waits
# is what helps. Measured at 400 rows a run, twice in each ordering to keep
# cache warming from writing the answer: one writer 4–16 rows/s, two writers
# 32.3 and 32.5 (two orderings, 0.6% apart), four writers 45–62. Four is where
# the evidence stops being noisy without pushing a shared instance harder.
_WRITERS = 4

# A single 400-row write took 24s warm and 97s cold, so the 30s the application
# pool uses would abandon the job on an ordinary slow batch. The work is
# resumable, but a restart re-walks the primary key from the start, and 30s is
# not a limit worth paying that for.
_WRITE_TIMEOUT = 600.0

_INDEX = "idx_vi_chunks_bm25"


async def _vector_pool(writers: int = 1):
    """A pool of this job's own, not the application's.

    The store's pool is sized for a web service and carries a 30-second
    command timeout; both are wrong here. A batch write takes tens of seconds
    by design, and `writers` of them run at once plus the reader — borrowing
    that many connections from the pool the request path shares would be
    taking them from the thing this migration is supposed to leave alone.
    """
    store = get_vector_store()
    if not isinstance(store, PgvectorStore):
        raise SystemExit(
            f"driver '{settings.vector_store_driver}' has no sparse_bm25 column; "
            "this backfill is pgvector-only."
        )
    dsn = store._dsn or settings.database_url
    return await asyncpg.create_pool(
        dsn, min_size=1, max_size=writers + 1, command_timeout=_WRITE_TIMEOUT,
    )


async def _column_exists(pool, schema: str) -> bool:
    async with pool.acquire() as c:
        return bool(await c.fetchval(
            """
            SELECT true FROM information_schema.columns
             WHERE table_schema = $1 AND table_name = 'chunks'
               AND column_name = 'sparse_bm25'
            """,
            schema,
        ))


def _owed(since: datetime | None, param: int) -> str:
    """What a pass would read — one expression, used by the sweep and the count.

    Without `--since` the queue is exactly the rows nothing has encoded. With
    it, the window is ADDED rather than substituted: a chunk inserted while the
    previous pass ran is NULL, and a chunk rewritten while it ran is stale with
    no NULL to mark it. A convergence pass that looked at only one of the two
    would leave the other behind with nothing left to find it.

    `param` is which placeholder carries the timestamp, because the two callers
    bind a different number of arguments before it.
    """
    if since is None:
        return "sparse_bm25 IS NULL"
    return f"(sparse_bm25 IS NULL OR indexed_at > ${param})"


async def _counts(pool, schema: str, since: datetime | None) -> tuple[int, int]:
    """(rows nothing has encoded, rows a pass would read)."""
    async with pool.acquire() as c:
        nulls = int(await c.fetchval(
            f'SELECT count(*) FROM "{schema}".chunks WHERE sparse_bm25 IS NULL'
        ))
        args = (since,) if since is not None else ()
        owed = int(await c.fetchval(
            f'SELECT count(*) FROM "{schema}".chunks WHERE {_owed(since, 1)}',
            *args,
        ))
    return nulls, owed


async def _prepare(pool, schema: str) -> None:
    """Add the column. Deliberately NOT the index — see `_build_index`."""
    async with pool.acquire() as c:
        await c.execute("CREATE EXTENSION IF NOT EXISTS vchord_bm25")
        t0 = time.monotonic()
        await c.execute(
            f'ALTER TABLE "{schema}".chunks '
            "ADD COLUMN IF NOT EXISTS sparse_bm25 bm25_catalog.bm25vector"
        )
        print(f"  column: ok ({(time.monotonic() - t0) * 1000:.1f}ms)")
        print("  index:  not yet — run --index after the sweep converges")


async def _build_index(pool, schema: str) -> None:
    """Build the index once, over a column that is already full.

    The order matters far more than it looks. Measured on a 2.1M-chunk corpus:
    one 500-row batch took **103 seconds** with the index present and **2.5
    seconds** without it — 42x, and at the first rate the sweep would take
    roughly five days. Every row written into an existing BM25 index pays that
    index's per-term maintenance, and the per-term cost is the dominant term in
    this index's size: at fixed posting count, a 68x larger vocabulary made the
    index 17.5x bigger, while doubling the rows at fixed vocabulary added 7%.
    Paying it two million times instead of once is the whole difference.

    So: `--prepare` adds the column, the sweep fills it, and this builds the
    index at the end. It is also why this cannot be `_do_ensure`'s job — that
    method runs its DDL inside one transaction under an advisory lock, and a
    build of this size would hold a ShareLock against every INSERT for its
    duration. `CREATE INDEX CONCURRENTLY` cannot run inside a transaction
    block, which is why this opens none.
    """
    async with pool.acquire() as c:
        nulls = int(await c.fetchval(
            f'SELECT count(*) FROM "{schema}".chunks WHERE sparse_bm25 IS NULL'
        ))
        if nulls:
            # Building over a half-filled column is not wrong, but it turns the
            # rest of the sweep into the slow kind. Refusing is the useful
            # answer: the operator has more sweeping to do.
            raise SystemExit(
                f"{nulls} row(s) still have no vector. Build the index after the "
                "sweep converges — with it in place each batch pays index "
                "maintenance per row, which measured 42x slower."
            )
        t0 = time.monotonic()
        await c.execute(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} '
            f'ON "{schema}".chunks USING bm25 (sparse_bm25 bm25_catalog.bm25_ops)'
        )
        valid = await c.fetchval(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass($1)",
            f'"{schema}".{_INDEX}',
        )
        size = await c.fetchval(
            "SELECT pg_size_pretty(pg_relation_size(to_regclass($1)))",
            f'"{schema}".{_INDEX}',
        )
        print(f"  index:  {'valid' if valid else 'INVALID'}  {size} "
              f"({time.monotonic() - t0:.1f}s)")
        if not valid:
            # A CONCURRENTLY build that loses its race leaves the index behind
            # marked invalid, and PostgreSQL will not use it. Saying so is the
            # point: a silent invalid index is a search that quietly stops
            # matching after the flip.
            raise SystemExit(
                f"{_INDEX} is INVALID — drop it and re-run --index "
                "(a CONCURRENTLY build that fails leaves it behind unusable)."
            )


async def _encode(content: str, gate: asyncio.Semaphore) -> str:
    """Exactly what the store would write for this chunk.

    Going through `encode_document` rather than reimplementing the raw-TF
    branch is deliberate: the weight convention is chosen from the shape in one
    place, and a backfill that encoded by its own rules would be a second
    opinion about what the corpus means.

    The gate bounds how many are in flight. Each call is one Kiwi job plus one
    `get_or_create_term_ids` round trip, and the batch is large enough that
    releasing all of them at once would queue hundreds of connections against
    a pool sized for a web service.
    """
    async with gate:
        idx, vals = await sparse_encoder.encode_document(
            content, sparse_shape="vchord"
        )
    return _bm25vector_literal(idx, vals)


async def _apply(pool, schema: str, rows) -> int:
    """Encode the batch and write it, skipping anything that moved underneath.

    One rule, and it is about content rather than about NULL: write this
    encoding only while the row still holds the text it was made from.
    `indexed_at` is that identity — every write through the store stamps it —
    so a chunk the indexer rewrote between this batch's read and its write is
    left with what the store gave it instead of being stamped with tokens from
    text it no longer has. The row is still owed, and the next pass's window
    opens before this one wrote anything.

    An earlier version also allowed the write when the row was still NULL. That
    reads as the safer rule and is in fact the weaker one: the case it was
    guarding (something filled the column without touching `indexed_at`) has no
    writer — the store always moves both together — while the clause it added
    would let a stale encoding land on a row whose column had been filled but
    whose content had not moved. Content identity is the whole question.
    """
    gate = asyncio.Semaphore(_CONCURRENCY)
    encoded = await asyncio.gather(
        *(_encode(r["content"] or "", gate) for r in rows)
    )
    sql = f"""
        UPDATE "{schema}".chunks c
           SET sparse_bm25 = m.v::bm25_catalog.bm25vector
          FROM (SELECT unnest($1::uuid[]) AS cid,
                       unnest($2::text[]) AS v,
                       unnest($3::timestamptz[]) AS ts) m
         WHERE c.chunk_id = m.cid AND c.indexed_at = m.ts
    """
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute(
                sql,
                [r["chunk_id"] for r in rows],
                list(encoded),
                [r["indexed_at"] for r in rows],
                timeout=_WRITE_TIMEOUT,
            )
    return int(res.split()[-1]) if res.startswith("UPDATE") else 0


async def _pass(pool, schema: str, since: datetime | None,
                writers: int = _WRITERS) -> int:
    """One forward sweep over the primary key.

    Keyset pagination rather than a bare `WHERE ... LIMIT`: with the latter,
    every batch restarts the scan and walks the rows already done, so the sweep
    costs O(n²) over a corpus this size. Carrying the last chunk_id keeps the
    whole run to one pass of the primary-key index.

    In `--since` mode it is doing more than that — it is what ends the loop.
    Writing `sparse_bm25` does not move `indexed_at`, by design, so a row this
    sweep just filled STILL satisfies the window it was read under. Only the
    cursor moving past it stops the query from handing it back forever.

    It is still not a resume cursor: nothing is persisted, and an interrupted
    run simply sweeps again and skips whatever is already filled.
    """
    sql = f"""
        SELECT chunk_id, content, indexed_at
          FROM "{schema}".chunks
         WHERE chunk_id > $1 AND {_owed(since, 2)}
         ORDER BY chunk_id
         LIMIT {_BATCH}
    """
    cursor = uuid.UUID(int=0)
    written = 0
    seen = 0
    started = time.monotonic()
    while True:
        async with pool.acquire() as c:
            args = (cursor, since) if since is not None else (cursor,)
            rows = await c.fetch(sql, *args)
        if not rows:
            break
        cursor = rows[-1]["chunk_id"]
        seen += len(rows)
        # Split the batch and write the parts at once. Encoding still happens
        # inside each part, so the tokenizer stays busy while the writes wait
        # on disk — which is the whole reason this is split.
        size = -(-len(rows) // writers)
        parts = [rows[i:i + size] for i in range(0, len(rows), size)]
        written += sum(await asyncio.gather(
            *(_apply(pool, schema, part) for part in parts)
        ))
        elapsed = time.monotonic() - started
        print(f"\r  {seen} read · {written} written · "
              f"{seen / elapsed:.0f}/s", end="", flush=True)
    if seen:
        print()
    return written


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backfill sparse_bm25 on pgvector points (akb#615)."
    )
    ap.add_argument("--check", action="store_true",
                    help="report what is left, then exit")
    ap.add_argument("--prepare", action="store_true",
                    help="add the column (not the index), then exit")
    ap.add_argument("--index", action="store_true",
                    help="build the index CONCURRENTLY once the sweep has "
                         "converged, then exit")
    ap.add_argument("--since", metavar="TIMESTAMP",
                    help="re-encode rows rewritten after this instant "
                         "(ISO 8601); use the start of the previous pass")
    ap.add_argument("--tokenizer-processes", type=int, default=None,
                    help="Kiwi process pool size (default: the app setting)")
    ap.add_argument("--writers", type=int, default=_WRITERS,
                    help=f"UPDATE statements in flight at once (default {_WRITERS}); "
                         "the write is disk-latency bound, so this is the knob "
                         "that matters")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        raise SystemExit("--since needs a timezone, e.g. ...T12:00:00+00:00")

    await init_db()
    schema = settings.vector_store_schema
    pool = None
    try:
        pool = await _vector_pool(max(1, args.writers))

        if args.prepare:
            await _prepare(pool, schema)
            return

        if args.index:
            if not await _column_exists(pool, schema):
                raise SystemExit(
                    f'"{schema}".chunks has no sparse_bm25 column — '
                    "run --prepare first."
                )
            await _build_index(pool, schema)
            return

        if not await _column_exists(pool, schema):
            raise SystemExit(
                f'"{schema}".chunks has no sparse_bm25 column — run --prepare first.'
            )

        if args.check:
            nulls, owed = await _counts(pool, schema, since)
            print(f"{nulls} never encoded"
                  + (f" · {owed} in a pass from {since.isoformat()}"
                     if since is not None else ""))
            return

        # The instant to hand the NEXT pass. Read before any work, so a chunk
        # rewritten while this pass runs falls inside the next one's window
        # rather than between the two.
        async with pool.acquire() as c:
            started_at = await c.fetchval("SELECT now()")

        sparse_encoder.start_tokenizer_pool(args.tokenizer_processes)
        try:
            written = await _pass(pool, schema, since, args.writers)
        finally:
            sparse_encoder.stop_tokenizer_pool()

        nulls, _ = await _counts(pool, schema, since)
        print(f"wrote {written} · {nulls} never encoded")

        # `written == 0` is the convergence signal, not the count above: after a
        # `--since` pass the rows it fixed still fall inside its own window, so
        # re-counting with the SAME instant can never reach zero. Each pass
        # hands the next one a later window, and the run that writes nothing is
        # the one that found nothing left.
        if written == 0 and nulls == 0:
            print("  nothing left in this window — safe to flip the shape, then "
                  "run once more with the instant below to catch the flip itself.")
        else:
            print("  not converged yet; run again with:")
        print(f"  python -m scripts.backfill_bm25_vector "
              f"--since '{started_at.isoformat()}'")
    finally:
        if pool is not None:
            await pool.close()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
