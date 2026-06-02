"""Migration 031: drop `memories` and `sessions` tables.

These tables backed the akb_remember/recall/forget MCP tools and the
session_start/session_end REST endpoints — both removed in v0.4.0. Agent
dedicated memory is now expressed as a per-user vault
(``agent-memory-{username}``) with per-session collections, driven by
the new ``/api/v1/agent-sessions`` REST surface and the AKB lifecycle
plugin family (``akb-claude-code``, ``akb-cursor``, …).

The drop is unconditional (``DROP TABLE IF EXISTS``) — these tables
have no FK references from other tables (verified at migration time),
so removal cannot cascade-break the schema. Existing rows are lost;
operators who want to retain the data should snapshot before
upgrading.

Idempotent: re-running the migration is a no-op once the tables are
gone.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.031")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # Order: indexes implicitly drop with their table. No FK pointing at
    # these tables exists in the schema, so a straight DROP is safe.
    await conn.execute("DROP TABLE IF EXISTS memories CASCADE")
    await conn.execute("DROP TABLE IF EXISTS sessions CASCADE")
    logger.info(
        "Migration 031 dropped legacy `memories` + `sessions` tables. "
        "Agent memory is now vault-shaped (agent-memory-{username})."
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
