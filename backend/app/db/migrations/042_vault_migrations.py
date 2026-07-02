"""Migration 042: add per-vault table migration catalog.

``vault_migrations`` records REST table migration idempotency keys and
checksums so replaying the same operation list is a no-op while reusing a
key for different operations is rejected.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.042")


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
            CREATE TABLE IF NOT EXISTS vault_migrations (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                checksum TEXT NOT NULL,
                UNIQUE(vault_id, name)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vault_migrations_vault_applied
                ON vault_migrations(vault_id, applied_at DESC)
            """
        )

    logger.info("Migration 042 added vault_migrations catalog")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
