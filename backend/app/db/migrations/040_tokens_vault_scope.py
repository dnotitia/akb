"""Migration 040: add `tokens.vault_scope` (per-PAT vault scope).

A PAT may carry a vault scope (`{prefixes, extra_vaults}` JSONB) so a
token can be restricted to a vault set independently of its user's ACL.
A request's effective WRITE permission is `user-ACL ∩ vault_scope` — an
intersection, so a scope only ever SUBTRACTS authority (escalation-
impossible by construction). Enforcement intersects the target vault
with this scope on mutating access checks; reads are unrestricted.

NULL = unscoped (the historical full-ACL behaviour) — every pre-existing
token row keeps NULL, so behaviour is unchanged.

Idempotent: `ADD COLUMN IF NOT EXISTS` so re-running is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.040")


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
              ADD COLUMN IF NOT EXISTS vault_scope JSONB
            """
        )

    logger.info(
        "Migration 040 added tokens.vault_scope "
        "(NULL = unscoped — existing tokens unaffected)"
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
