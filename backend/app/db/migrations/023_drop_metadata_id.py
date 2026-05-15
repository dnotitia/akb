"""Migration 023: strip the legacy `id` key from documents.metadata.

The `metadata.id` field (d-prefix short id like `d-a3f29b81`) was used
as a third lookup arm in `find_by_ref` alongside UUID and path. After
the URI-canonical cutover, MCP / REST clients address documents by
URI and the handler splits that into (vault, path) before hitting the
DB — the d-prefix arm is no longer reachable. The matching SQL arms
in document_repo / kg_service / search_service have all been dropped.

This migration removes the now-dead key from existing rows. Idempotent
via a NULL-check (running it twice does nothing).

Cosmetic: no functional change — the SQL queries that referenced
`metadata->>'id'` are already gone, so leaving stale values is harmless.
Stripping them keeps `metadata` JSONB introspection clean.

Note: `.md` files in the vault's git history still carry the `id:`
yaml frontmatter line on commits made before PR6 (feat/drop-d-prefix).
Git history is immutable; new commits won't add it, and the parser
ignores unknown keys when reading old commits back via akb_get.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.023")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # Count rows that still carry the legacy key (for logging only).
    remaining = await conn.fetchval(
        "SELECT COUNT(*) FROM documents WHERE metadata ? 'id'"
    )
    if not remaining:
        logger.info(
            "Migration 023: no documents carry the legacy `metadata.id` key; "
            "nothing to strip"
        )
        return

    async with conn.transaction():
        result = await conn.execute(
            "UPDATE documents SET metadata = metadata - 'id' WHERE metadata ? 'id'"
        )
        try:
            updated = int(result.split()[-1])
        except (ValueError, IndexError):
            updated = -1

        logger.info(
            "Migration 023 stripped legacy `metadata.id` from %d documents "
            "(canonical handle is the akb:// URI)", updated,
        )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
