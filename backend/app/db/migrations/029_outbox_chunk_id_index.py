"""Migration 029: partial index on vector_delete_outbox(chunk_id).

The abandoned-chunk reaper (delete_worker._reap_abandoned_chunks_once)
guards its outbox INSERT with `WHERE NOT EXISTS (SELECT 1 FROM
vector_delete_outbox o WHERE o.chunk_id = a.id AND o.processed_at IS NULL)`
to avoid duplicating rows for chunks already enqueued by an explicit
delete. The only existing index on the outbox is on `next_attempt_at`
filtered to unprocessed rows, so the chunk_id lookup falls through to a
filtered seq scan — fine for a fresh queue, O(pending) for a backed-up
one.

This partial index also speeds up future ON CONFLICT or NOT EXISTS
guards in `_drop_source_chunks_with_outbox` and `delete_vault_chunks`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.029")


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
        CREATE INDEX IF NOT EXISTS idx_vector_outbox_chunk_pending
            ON vector_delete_outbox(chunk_id)
         WHERE processed_at IS NULL
        """
    )
    logger.info(
        "Migration 029 added idx_vector_outbox_chunk_pending "
        "(partial index on chunk_id for reaper dedup)."
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
