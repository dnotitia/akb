"""Migration 079: crash-safe claim lifecycle for durable worker queues.

Every retry counter now represents work *claimed*, not only failures that
managed to reach their exception handler.  The companion ``*_claimed_at`` and
``*_abandoned_at`` timestamps let an operator distinguish an active lease from
a terminal row and let the queue rescuer close a claim whose process died on
its final attempt.

Idempotent and additive.  Existing completed rows are normalised to retry=0;
pending rows retain their historical retry count.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.079")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    await conn.execute(
        """
        ALTER TABLE chunks
            ADD COLUMN IF NOT EXISTS vector_claimed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS vector_abandoned_at TIMESTAMPTZ;

        ALTER TABLE vector_delete_outbox
            ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS abandoned_at TIMESTAMPTZ;

        ALTER TABLE s3_delete_outbox
            ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS abandoned_at TIMESTAMPTZ;

        ALTER TABLE events
            ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS abandoned_at TIMESTAMPTZ;

        ALTER TABLE documents
            ADD COLUMN IF NOT EXISTS llm_claimed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS llm_abandoned_at TIMESTAMPTZ;
        """
    )

    # Success is a fresh retry epoch.  This also makes the migration safe for
    # rows written by the old failure-attempt accounting implementation.
    await conn.execute(
        """
        UPDATE chunks
           SET vector_retry_count = 0,
               vector_claimed_at = NULL,
               vector_abandoned_at = NULL
         WHERE vector_indexed_at IS NOT NULL;

        UPDATE vector_delete_outbox
           SET retry_count = 0, claimed_at = NULL, abandoned_at = NULL
         WHERE processed_at IS NOT NULL;

        UPDATE s3_delete_outbox
           SET retry_count = 0, claimed_at = NULL, abandoned_at = NULL
         WHERE processed_at IS NOT NULL;

        UPDATE events
           SET attempts = 0, claimed_at = NULL, abandoned_at = NULL
         WHERE redis_published_at IS NOT NULL;

        UPDATE documents
           SET llm_retry_count = 0,
               llm_claimed_at = NULL,
               llm_abandoned_at = NULL
         WHERE llm_metadata_at IS NOT NULL;
        """
    )

    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunks_vector_claim_lifecycle
            ON chunks (vector_next_attempt_at, created_at DESC, id)
         WHERE vector_indexed_at IS NULL AND vector_abandoned_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_vector_delete_claim_lifecycle
            ON vector_delete_outbox (next_attempt_at, id)
         WHERE processed_at IS NULL AND abandoned_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_s3_delete_claim_lifecycle
            ON s3_delete_outbox (next_attempt_at, id)
         WHERE processed_at IS NULL AND abandoned_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_events_claim_lifecycle
            ON events (next_attempt_at, id)
         WHERE redis_published_at IS NULL AND abandoned_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_documents_llm_claim_lifecycle
            ON documents (llm_next_attempt_at, id)
         WHERE source = 'external_git'
           AND llm_metadata_at IS NULL
           AND llm_abandoned_at IS NULL;
        """
    )
    logger.info("Migration 079 applied: crash-safe worker claim lifecycle")
