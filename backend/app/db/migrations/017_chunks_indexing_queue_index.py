"""Migration 017: partial index on chunks for the indexing-queue claim.

embed_worker._claim_batch orders by `created_at DESC, id` over rows
where `vector_indexed_at IS NULL`. Without a matching partial index
the planner does a full sort on the chunks table — fine at 10k rows,
expensive at 600k+ during a re-index window (every worker tick reads
+ sorts the whole pending set, just to grab the top 16).

The existing `idx_chunks_vector_pending` is on
`vector_next_attempt_at` and serves the retry-eligibility predicate
but doesn't help ordering. Add a sibling that does both:

    ORDER BY created_at DESC, id
    WHERE vector_indexed_at IS NULL

Idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.017")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunks_indexing_queue
            ON chunks (created_at DESC, id)
         WHERE vector_indexed_at IS NULL
        """
    )
    logger.info("Migration 017 applied: idx_chunks_indexing_queue")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
