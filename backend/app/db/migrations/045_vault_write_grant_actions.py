"""Migration 045: operation limits for managed Vault write grants.

Existing grants are intentionally broad and must retain their behavior. The
new ``write_actions`` column therefore defaults every existing and future
body-less grant to ``['*']``. New callers may replace that wildcard with one
or more registered operation names, initially ``file_upload``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.045")


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
            ALTER TABLE vault_write_grants
                ADD COLUMN IF NOT EXISTS write_actions TEXT[]
                NOT NULL DEFAULT ARRAY['*']::TEXT[]
            """
        )
        await conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                      FROM pg_constraint
                     WHERE conname = 'vault_write_grants_actions_nonempty'
                       AND conrelid = 'vault_write_grants'::regclass
                ) THEN
                    ALTER TABLE vault_write_grants
                        ADD CONSTRAINT vault_write_grants_actions_nonempty
                        CHECK (
                            cardinality(write_actions) > 0
                            AND NOT (write_actions @> ARRAY['']::TEXT[])
                        );
                END IF;
            END
            $$
            """
        )

    logger.info("Migration 045 added operation limits to vault_write_grants")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
