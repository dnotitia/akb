"""Migration 111: durable BM25 recompute progress — `bm25_recompute_run` and
`bm25_recompute_terms`.

`recompute_stats()` accumulated a whole corpus scan in a session-scoped
temporary table and wrote nothing until the scan finished. Four things lived
and died with the process: that table, the running `total_docs` /
`total_length` / `source_chunk_count`, the keyset cursor, and the
`source_revision` captured before the scan. On a large corpus the scan is the
expensive part by orders of magnitude and worker shutdown is an absolute
deadline, so a routine deployment reliably destroyed it and the next tick
started at the first chunk again (akb#616).

These two tables move that state out of the session:

  * `bm25_recompute_run` — one row describing the run in flight. The cursor
    says where the scan is; the three counters are the partial totals; the
    tokenizer identity says whether a later process may resume this work at
    all; `source_revision` is the invalidation boundary captured when the run
    FIRST started and deliberately carried across resumes, because
    re-capturing it would narrow the window in which chunks written during the
    scan are revisited.
  * `bm25_recompute_terms` — one row per term, the partial document frequency.

A batch writes its term contributions and its advanced cursor in one
transaction, so an interruption costs one batch rather than the whole run. The
final publish stays atomic and unchanged: half a corpus's `df` is worse than
none, and the publish is seconds against hours of scan.

The accumulator is deliberately NOT called `bm25_recompute_df`. That is the
name of the temporary table the previous implementation used, and its startup
path ran `DROP TABLE IF EXISTS bm25_recompute_df` BEFORE creating the
temporary one — at which point the name resolves to a permanent table if one
exists. During a rolling deploy a pod still running the old code would
therefore have deleted a new pod's accumulated progress. A different name
makes the two implementations unable to touch each other's state.

Both tables are scratch space for a recalculable result, so nothing here is
backed up or migrated: a lost row means one recompute starts over, which is
exactly the behaviour being replaced and never a data loss.

Idempotent: `CREATE TABLE IF NOT EXISTS`, so re-running is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.111")

DDL = """
CREATE TABLE IF NOT EXISTS bm25_recompute_run (
    id                 smallint    PRIMARY KEY DEFAULT 1,
    tokenizer_name     text        NOT NULL,
    tokenizer_version  text        NOT NULL,
    source_revision    bigint      NOT NULL,
    cursor_chunk_id    uuid,
    total_docs         bigint      NOT NULL DEFAULT 0,
    total_length       bigint      NOT NULL DEFAULT 0,
    source_chunk_count bigint      NOT NULL DEFAULT 0,
    resumed            integer     NOT NULL DEFAULT 0,
    started_at         timestamptz NOT NULL DEFAULT NOW(),
    updated_at         timestamptz NOT NULL DEFAULT NOW(),
    CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS bm25_recompute_terms (
    term text   PRIMARY KEY,
    df   bigint NOT NULL
);
"""


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn) -> None:
    await conn.execute(DDL)
    logger.info(
        "Migration 111 created bm25_recompute_run + bm25_recompute_terms"
    )


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        await init_db()
        await migrate()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
