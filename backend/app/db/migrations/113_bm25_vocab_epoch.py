"""Migration 113: the BM25 vocabulary epoch — `bm25_vocab_epoch`.

Term ids in `bm25_vocab` are dense, and one command reassigns them:
`scripts/compact_bm25_term_ids.py`. It rewrites the vocabulary and every stored
vector in one transaction, and advances this epoch in that same transaction.

Encoding a chunk and storing it are two transactions. An encoder that read its
ids before a renumbering committed, and a writer that stores them after, would
put the old numbering on the new one: a silent corruption. So the encoder
reports the epoch its ids were read at, and the writer re-reads the epoch under
the term-id fence (`bm25_maintenance.BM25_VOCAB_EPOCH_LOCK_KEY`, shared) in the
transaction that stores them. A different epoch sends the chunk back to be
encoded again.

One row, zero on a new database. Nothing but the renumbering writes it.

Skipped where the vocabulary itself is absent, as migration 084 is: a
historical bootstrap fixture records 005 as applied without its tables.

Idempotent: `CREATE TABLE IF NOT EXISTS` and `ON CONFLICT DO NOTHING`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.113")

DDL = """
CREATE TABLE IF NOT EXISTS bm25_vocab_epoch (
    id     smallint PRIMARY KEY DEFAULT 1,
    epoch  bigint   NOT NULL DEFAULT 0,
    CHECK (id = 1)
);

INSERT INTO bm25_vocab_epoch (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
"""


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn) -> None:
    if not await conn.fetchval("SELECT to_regclass('public.bm25_vocab') IS NOT NULL"):
        logger.info("Migration 113: bm25_vocab is unavailable; skipping")
        return
    await conn.execute(DDL)
    logger.info("Migration 113 created bm25_vocab_epoch")


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        await init_db()
        await migrate()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
