"""Migration: add retry tracking columns to `chunks` for embedding backfill.

When the embedding API is unavailable, chunks get stored with NULL embedding
(graceful degradation in index_service.generate_embeddings). A background
worker (app.services.embed_worker) polls for such chunks and retries with
exponential backoff. These columns let the worker:

- track how many attempts have been made (`embed_retry_count`)
- schedule the next retry (`embed_next_attempt_at`)
- surface the last error for debugging (`embed_last_error`)

Idempotent — safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.004")


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
        ALTER TABLE chunks
            ADD COLUMN IF NOT EXISTS embed_retry_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS embed_last_error TEXT,
            ADD COLUMN IF NOT EXISTS embed_next_attempt_at TIMESTAMPTZ
        """
    )

    # Post-Phase-4 (migration 016) the embedding column is gone — the
    # bootstrap UPDATE + partial index would fail with
    # UndefinedColumnError. Skip them on a post-016 schema; the embed_*
    # columns themselves get dropped by 016 anyway.
    has_embedding = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'chunks'
               AND column_name = 'embedding'
        )
        """
    )
    if not has_embedding:
        logger.info(
            "Migration 004: chunks.embedding already dropped (post-016); "
            "skipping bootstrap UPDATE + partial index"
        )
        return

    # Bootstrap: any existing chunk without embedding is eligible for
    # immediate retry (no scheduled time yet).
    await conn.execute(
        """
        UPDATE chunks
           SET embed_next_attempt_at = NOW()
         WHERE embedding IS NULL
           AND embed_next_attempt_at IS NULL
        """
    )

    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunks_embed_pending
            ON chunks (embed_next_attempt_at)
         WHERE embedding IS NULL
        """
    )

    logger.info("Migration 004 applied: chunks embed retry columns added")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
