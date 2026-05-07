"""Migration 012: drop `llm_metadata_cache`.

The cache was added in migration 010 to skip duplicate LLM calls when
the same git blob appears in multiple paths / vaults / lifecycle
events. In practice its hit rate is negligible at single-mirror scale
(legal documents are unique, renames rare), and it adds a table +
per-worker lookup for a handful of saved LLM calls per year.

Removal simplifies the metadata_worker path to one LLM call per doc
and drops a schema object that would otherwise need TTL/GC policy.

Idempotent — safe on DBs where migration 010 has or hasn't landed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.012")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    await conn.execute("DROP TABLE IF EXISTS llm_metadata_cache")
    logger.info("Migration 012 applied: llm_metadata_cache dropped")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
