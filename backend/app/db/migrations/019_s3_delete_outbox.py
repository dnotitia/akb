"""Migration 019: s3_delete_outbox table.

Mirrors the `vector_delete_outbox` shape so file deletions can be
atomic with the `vault_files` row removal: the service writes the
DB DELETE + an outbox INSERT in the same TX, then a background
worker drains the outbox and removes the underlying S3 object.

A crash between the DB commit and the S3 call cannot leave the system
inconsistent — the outbox row survives and the worker retries.

Idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.019")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    exists = await conn.fetchval(
        """
        SELECT 1
          FROM information_schema.tables
         WHERE table_schema = 'public'
           AND table_name   = 's3_delete_outbox'
        """
    )
    if exists:
        logger.info(
            "Migration 019 already applied (s3_delete_outbox exists); skipping"
        )
        return

    await conn.execute(
        """
        CREATE TABLE s3_delete_outbox (
            id              BIGSERIAL PRIMARY KEY,
            s3_key          TEXT NOT NULL,
            enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processed_at    TIMESTAMPTZ,
            retry_count     INT NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ,
            last_error      TEXT
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX idx_s3_delete_pending
            ON s3_delete_outbox (next_attempt_at)
         WHERE processed_at IS NULL
        """
    )
    await conn.execute(
        """
        CREATE INDEX idx_s3_delete_processed
            ON s3_delete_outbox (processed_at)
         WHERE processed_at IS NOT NULL
        """
    )
    logger.info("Migration 019 applied: created s3_delete_outbox")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
