"""Durably reconcile S3-backed File mutations into Native text projection."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.088")


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
            CREATE TABLE IF NOT EXISTS native_file_projection_outbox (
                file_id UUID PRIMARY KEY,
                intent_id UUID NOT NULL UNIQUE,
                namespace_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                source_present BOOLEAN NOT NULL,
                logical_path TEXT NOT NULL,
                mime_type TEXT,
                content_hash TEXT,
                byte_size BIGINT,
                s3_key TEXT,
                actor TEXT NOT NULL,
                generation BIGINT NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                claimed_at TIMESTAMPTZ,
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                outcome TEXT,
                last_error TEXT,
                CONSTRAINT native_file_projection_path_check
                    CHECK (btrim(logical_path) <> ''),
                CONSTRAINT native_file_projection_actor_check
                    CHECK (btrim(actor) <> ''),
                CONSTRAINT native_file_projection_generation_check
                    CHECK (generation > 0),
                CONSTRAINT native_file_projection_retry_check
                    CHECK (retry_count >= 0),
                CONSTRAINT native_file_projection_source_shape_check
                    CHECK (
                        (
                            source_present
                            AND btrim(COALESCE(mime_type, '')) <> ''
                            AND content_hash ~ '^[0-9a-f]{64}$'
                            AND byte_size >= 0
                            AND btrim(COALESCE(s3_key, '')) <> ''
                        )
                        OR
                        (
                            NOT source_present
                            AND mime_type IS NULL
                            AND content_hash IS NULL
                            AND byte_size IS NULL
                            AND s3_key IS NULL
                        )
                    ),
                CONSTRAINT native_file_projection_completion_shape_check
                    CHECK (
                        (completed_at IS NULL AND outcome IS NULL)
                        OR (completed_at IS NOT NULL AND btrim(COALESCE(outcome, '')) <> '')
                    )
            );

            CREATE INDEX IF NOT EXISTS idx_native_file_projection_pending
                ON native_file_projection_outbox(next_attempt_at, created_at, file_id)
                WHERE completed_at IS NULL;
            """
        )
    logger.info("Migration 088: Native File projection outbox ready")
