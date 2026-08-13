"""Migration 034: retain the legacy ``oidc_transients`` schema.

The dedicated Phase 2 product-admin client uses a namespaced, single-use
``admin-state-v1`` record whose payload also binds the initiating browser by
an opaque-cookie hash. Ordinary SSO browser routes remain staged and may reuse
a different kind only as part of the Phase 4 browser-session design.

Idempotent: ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.034")


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
            CREATE TABLE IF NOT EXISTS oidc_transients (
                key         TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
                expires_at  TIMESTAMPTZ NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_oidc_transients_expiry "
            "ON oidc_transients(expires_at)"
        )

    logger.info("Migration 034 ensured oidc_transients table")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
