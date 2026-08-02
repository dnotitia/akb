"""Migration 050: guarded native searchable-document derived state.

The native ledger remains authoritative.  These relations only bind rebuildable
chunk ids to the exact native Revision consumed from a durable invalidation
intent.  ``native_document`` is deliberately distinct from legacy ``document``
so selecting one authority cannot accidentally hydrate the other.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.050")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE chunks
                DROP CONSTRAINT IF EXISTS chunks_source_type_check;
            ALTER TABLE chunks
                ADD CONSTRAINT chunks_source_type_check
                CHECK (source_type IN ('document', 'native_document', 'table', 'file'));

            ALTER TABLE native_invalidation_intents
                ADD COLUMN IF NOT EXISTS delivery_outcome TEXT,
                ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
            ALTER TABLE native_invalidation_intents
                DROP CONSTRAINT IF EXISTS native_invalidation_delivery_outcome_check;
            ALTER TABLE native_invalidation_intents
                ADD CONSTRAINT native_invalidation_delivery_outcome_check
                CHECK (delivery_outcome IS NULL OR delivery_outcome IN (
                    'applied', 'deleted', 'superseded', 'direct_grep'
                ));
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'native_invalidation_intents_delivery_link_key'
                       AND conrelid = 'native_invalidation_intents'::regclass
                ) THEN
                    ALTER TABLE native_invalidation_intents
                        ADD CONSTRAINT native_invalidation_intents_delivery_link_key
                        UNIQUE (intent_id, namespace_id, resource_id, revision_id);
                END IF;
            END;
            $$;

            CREATE INDEX IF NOT EXISTS idx_native_invalidation_delivery_pending
                ON native_invalidation_intents(next_attempt_at, occurred_at, intent_id)
                WHERE completed_at IS NULL;

            CREATE TABLE IF NOT EXISTS native_derived_heads (
                resource_id UUID PRIMARY KEY,
                namespace_id UUID NOT NULL,
                revision_id TEXT NOT NULL,
                intent_id UUID NOT NULL UNIQUE,
                path TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                settled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT native_derived_heads_digest_shape
                    CHECK (content_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_derived_heads_chunk_count
                    CHECK (chunk_count >= 0),
                CONSTRAINT native_derived_heads_revision_fkey
                    FOREIGN KEY (namespace_id, resource_id, revision_id)
                    REFERENCES native_revisions(namespace_id, resource_id, revision_id)
                    ON DELETE CASCADE,
                CONSTRAINT native_derived_heads_intent_fkey
                    FOREIGN KEY (intent_id, namespace_id, resource_id, revision_id)
                    REFERENCES native_invalidation_intents(
                        intent_id, namespace_id, resource_id, revision_id
                    ) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS native_derived_chunks (
                chunk_id UUID PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                namespace_id UUID NOT NULL,
                resource_id UUID NOT NULL,
                revision_id TEXT NOT NULL,
                intent_id UUID NOT NULL,
                CONSTRAINT native_derived_chunks_head_fkey
                    FOREIGN KEY (resource_id)
                    REFERENCES native_derived_heads(resource_id) ON DELETE CASCADE,
                CONSTRAINT native_derived_chunks_revision_fkey
                    FOREIGN KEY (namespace_id, resource_id, revision_id)
                    REFERENCES native_revisions(namespace_id, resource_id, revision_id)
                    ON DELETE CASCADE,
                CONSTRAINT native_derived_chunks_intent_fkey
                    FOREIGN KEY (intent_id, namespace_id, resource_id, revision_id)
                    REFERENCES native_invalidation_intents(
                        intent_id, namespace_id, resource_id, revision_id
                    ) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_native_derived_chunks_revision
                ON native_derived_chunks(resource_id, revision_id);
            """
        )
    logger.info("Migration 050: native searchable derived state ready")
