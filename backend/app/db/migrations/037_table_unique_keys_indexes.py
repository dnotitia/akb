"""Migration 037: add `vault_tables.unique_keys` + `vault_tables.indexes`.

AKB #215 extends the table DDL tools with declarative UNIQUE keys and
secondary indexes. The resolved metadata (the generated/validated names
+ columns) is persisted on the registry row so introspection
(`list_tables`, browse) can surface it and drop-by-name works.

Both columns are JSONB lists defaulting to `'[]'`, so every pre-existing
table row keeps its current (empty) metadata and behaviour is unchanged.

`check_constraints` is intentionally NOT added here — it ships with the
follow-up PR's own migration so each PR's schema change stays
self-contained.

Idempotent: `ADD COLUMN IF NOT EXISTS` so re-running is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.037")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    existing = await conn.fetchval(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'vault_tables'
           AND column_name = 'unique_keys'
        """
    )
    if existing:
        logger.info(
            "Migration 037: vault_tables.unique_keys already exists; skipping"
        )
        return

    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE vault_tables
              ADD COLUMN IF NOT EXISTS unique_keys JSONB NOT NULL DEFAULT '[]',
              ADD COLUMN IF NOT EXISTS indexes JSONB NOT NULL DEFAULT '[]'
            """
        )

    logger.info(
        "Migration 037 added vault_tables.unique_keys + .indexes "
        "(default '[]' — existing tables unaffected)"
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
