"""Migration 008: drop the legacy `document_id` columns from chunks and
vector_delete_outbox.

After migration 006, every chunk row carries `source_type` + `source_id`;
the app no longer needs `chunks.document_id` as a separate identifier.
Dropping the column also removes the FK CASCADE that was cleaning up
chunks when a document was deleted — the application code takes that
over (document_service.delete now calls delete_document_chunks
explicitly).

PG is the source of truth. Qdrant payload still carries both
`document_id` (legacy) and `source_id` keys on older points; the
worker will rewrite them during the post-migration reindex. Until
that rewrite is complete, ACL-scoped search may miss unreindexed
points — an acceptable temporary degradation given PG is intact.

Idempotent.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.008")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            "ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_document_id_fkey"
        )
        await conn.execute("DROP INDEX IF EXISTS idx_chunks_document")
        await conn.execute(
            "ALTER TABLE chunks DROP COLUMN IF EXISTS document_id"
        )
        await conn.execute(
            "ALTER TABLE vector_delete_outbox DROP COLUMN IF EXISTS document_id"
        )
    logger.info("Migration 008 applied: chunks/outbox legacy document_id dropped")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
