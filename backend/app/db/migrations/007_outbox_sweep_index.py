"""Migration 007: partial BTREE index to support the outbox sweeper.

The existing `idx_vector_delete_pending` covers `processed_at IS NULL`
for the worker's claim path. The sweep path wants the opposite — rows
that have been processed more than a day ago — so we add a partial
index on the non-NULL subset. Without it `DELETE ... WHERE processed_at
< NOW() - INTERVAL '1 day'` degenerates to a full table scan as the
outbox grows.

Idempotent.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.007")


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
        CREATE INDEX IF NOT EXISTS idx_vector_delete_processed
            ON vector_delete_outbox (processed_at)
         WHERE processed_at IS NOT NULL
        """
    )
    logger.info("Migration 007 applied: outbox sweep index")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
