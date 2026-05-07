"""Migration 006: generalize chunks to index documents, tables, and files.

Additive. Existing chunk rows get `source_type='document'` and
`source_id=document_id` via backfill — no data loss. `document_id` is
relaxed to NULL-able so chunks for tables/files can reuse the same row
shape. The existing `chunks.document_id` column is kept intact; callers
that filter by document still work. New code reads/writes via
`source_type` + `source_id`.

Idempotent — safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.006")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # Wrap backfill + SET NOT NULL in a single transaction so a concurrent
    # INSERT via the legacy path can't land a NULL source_id between the
    # UPDATE and the SET NOT NULL and crash the migration.
    # When migration 008 already ran (or this is a fresh DB where
    # init.sql defines chunks without document_id), there is nothing to
    # backfill — skip the legacy reads so the migration stays idempotent.
    chunks_has_document_id = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name='chunks' AND column_name='document_id'
        )
        """
    )

    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE chunks
                ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'document',
                ADD COLUMN IF NOT EXISTS source_id UUID
            """
        )
        if chunks_has_document_id:
            await conn.execute(
                "UPDATE chunks SET source_id = document_id WHERE source_id IS NULL"
            )
        await conn.execute(
            "ALTER TABLE chunks ALTER COLUMN source_id SET NOT NULL"
        )
        if chunks_has_document_id:
            await conn.execute(
                "ALTER TABLE chunks ALTER COLUMN document_id DROP NOT NULL"
            )

    await conn.execute(
        "ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_source_type_check"
    )
    await conn.execute(
        """
        ALTER TABLE chunks
            ADD CONSTRAINT chunks_source_type_check
            CHECK (source_type IN ('document', 'table', 'file'))
        """
    )

    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunks_source
            ON chunks (source_type, source_id)
        """
    )

    # Generalize the delete outbox the same way so vector_indexer can
    # process deletions for tables and files using the same machinery.
    outbox_has_document_id = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name='vector_delete_outbox' AND column_name='document_id'
        )
        """
    )
    await conn.execute(
        """
        ALTER TABLE vector_delete_outbox
            ADD COLUMN IF NOT EXISTS source_type TEXT,
            ADD COLUMN IF NOT EXISTS source_id UUID
        """
    )
    if outbox_has_document_id:
        await conn.execute(
            """
            UPDATE vector_delete_outbox
               SET source_type = 'document',
                   source_id = document_id
             WHERE source_id IS NULL
            """
        )

    logger.info("Migration 006 applied: chunks generalized (source_type/source_id)")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
