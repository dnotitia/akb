"""Migration 032: drop the never-used documents.supersedes column.

The `supersedes UUID REFERENCES documents(id)` column (and the matching
`supersedes` frontmatter field) was added for a document-supersession
lifecycle that was never operationalized — no code ever read or wrote it,
and the "superseded" status value it paired with was removed in 0.4.4 when
document status was leaned down to draft/active/archived. Dropping the
column also drops its self-referential FK constraint.

Idempotent: `DROP COLUMN IF EXISTS` is a no-op once the column is gone (and
on fresh installs, where init.sql no longer creates it).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.032")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    await conn.execute("ALTER TABLE documents DROP COLUMN IF EXISTS supersedes")
    logger.info("Migration 032: dropped unused documents.supersedes column.")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
