"""Migration 041: add tokens.key_class (pat/service/publishable).

Existing rows become ``pat`` so current JWT/PAT behavior is unchanged.
``service`` is the BFF/server credential class that AKB-038 can trust for
claim injection. ``publishable`` is a reserved seam for a future
browser-direct flow; the DB accepts the value but issuance rejects it for now.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.041")


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
            """
            ALTER TABLE tokens
              ADD COLUMN IF NOT EXISTS key_class TEXT NOT NULL DEFAULT 'pat'
            """
        )
        await conn.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'tokens_key_class_check'
                   AND conrelid = 'tokens'::regclass
              ) THEN
                ALTER TABLE tokens
                  ADD CONSTRAINT tokens_key_class_check
                  CHECK (key_class IN ('pat', 'service', 'publishable'));
              END IF;
            END $$;
            """
        )

    logger.info(
        "Migration 041 added tokens.key_class "
        "(pat default; service enabled; publishable reserved)"
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
