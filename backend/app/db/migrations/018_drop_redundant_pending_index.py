"""Migration 018: drop the now-redundant idx_chunks_vector_pending.

Migration 005 created `idx_chunks_vector_pending` on
`(vector_next_attempt_at) WHERE vector_indexed_at IS NULL` to speed up
the indexing-queue claim. Migration 017 added
`idx_chunks_indexing_queue` on `(created_at DESC, id) WHERE
vector_indexed_at IS NULL` — covering both the new ORDER BY and the
same WHERE filter. The planner selects the latter; the former just
costs INSERT/UPDATE bandwidth without serving any query.

Idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.018")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    await conn.execute("DROP INDEX IF EXISTS idx_chunks_vector_pending")
    logger.info("Migration 018 applied: dropped idx_chunks_vector_pending")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
