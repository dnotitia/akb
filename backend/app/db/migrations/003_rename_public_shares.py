"""Migration: rename public_shares → publications.

The "publish" / "share" terminology was inconsistent across the codebase.
"share" implies giving access to specific people, but the feature actually
makes content publicly accessible to anyone with the URL — that's a
"publish" operation, and the resulting object is a "publication".

This migration renames the table and all its indexes. Idempotent — safe
to run repeatedly.

Run:  python -m app.db.migrations.003_rename_public_shares
Auto: Called from app.db.postgres.init_db() after schema creation.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.003")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    publications_exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='publications')"
    )
    public_shares_exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='public_shares')"
    )

    # Case 1: only `publications` exists — already migrated, nothing to do.
    if publications_exists and not public_shares_exists:
        logger.info("Migration 003 already applied (publications exists, public_shares absent)")
        return

    # Case 2: neither exists — this is a fresh DB before init.sql ran. Skip.
    if not publications_exists and not public_shares_exists:
        logger.info("Neither publications nor public_shares exists — nothing to rename")
        return

    # Case 3: both exist. This happens when init.sql created an empty
    # `publications` table on a DB that still has the old `public_shares`.
    # If `publications` is empty, drop it and proceed with the rename.
    if publications_exists and public_shares_exists:
        publications_count = await conn.fetchval("SELECT COUNT(*) FROM publications")
        if publications_count > 0:
            logger.error(
                "Both publications (%d rows) and public_shares exist with data. "
                "Manual intervention required.",
                publications_count,
            )
            return
        logger.info("Empty `publications` shadow table found, dropping before rename")
        # Drop the empty shadow table and its indexes — they'll be re-created
        # by the rename below.
        await conn.execute("DROP TABLE publications CASCADE")

    # Case 4 (or after dropping shadow): only `public_shares` exists. Rename it.
    await conn.execute("ALTER TABLE public_shares RENAME TO publications")
    for old, new in [
        ("idx_public_shares_slug", "idx_publications_slug"),
        ("idx_public_shares_vault", "idx_publications_vault"),
        ("idx_public_shares_document", "idx_publications_document"),
        ("idx_public_shares_file", "idx_publications_file"),
        ("idx_public_shares_expires", "idx_publications_expires"),
    ]:
        await conn.execute(f"ALTER INDEX IF EXISTS {old} RENAME TO {new}")
    logger.info("Renamed public_shares → publications (and indexes)")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
