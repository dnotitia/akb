"""Migration: documents.is_public/public_slug → public_shares table.

1. Copy all existing published documents into the new unified public_shares table.
2. Drop the legacy documents.is_public, documents.public_slug columns and index.

This migration is idempotent — safe to run multiple times.

Run:  python -m app.db.migrations.002_public_shares
Auto: Called from app.db.postgres.init_db() after schema creation.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.002")


async def migrate(conn=None):
    """Run the migration. If `conn` is provided, run within that connection.
    Otherwise acquire a connection from the pool.
    """
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # The gate for this migration is whether the legacy columns still exist
    # on the documents table. The target table (public_shares or its renamed
    # successor `publications`) is created by init.sql before this runs.
    has_legacy_columns = await conn.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'documents' AND column_name = 'is_public'
        )
        """
    )
    if not has_legacy_columns:
        logger.info("Migration 002 already applied (legacy columns absent)")
        return

    # The target table for the copy must exist. After 003 it's `publications`,
    # before 003 it's `public_shares`. Pick whichever is present.
    target = await conn.fetchval(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ('publications', 'public_shares') "
        "ORDER BY (table_name = 'publications') DESC LIMIT 1"
    )
    if not target:
        logger.warning("Neither publications nor public_shares exists — skipping migration 002.")
        return

    rows = await conn.fetch(
        """
        SELECT d.id, d.vault_id, d.public_slug, d.title
        FROM documents d
        WHERE d.is_public = true AND d.public_slug IS NOT NULL
        """
    )

    migrated = 0
    for r in rows:
        try:
            await conn.execute(
                f"""
                INSERT INTO {target}
                    (slug, vault_id, resource_type, document_id, title)
                VALUES ($1, $2, 'document', $3, $4)
                ON CONFLICT (slug) DO NOTHING
                """,
                r["public_slug"], r["vault_id"], r["id"], r["title"],
            )
            migrated += 1
        except Exception as e:
            logger.warning("Failed to migrate doc %s: %s", r["id"], e)

    if migrated > 0:
        logger.info("Migrated %d published documents → %s", migrated, target)
    else:
        logger.info("No published documents to migrate")

    # Drop legacy index and columns
    await conn.execute("DROP INDEX IF EXISTS idx_documents_public_slug")
    await conn.execute("ALTER TABLE documents DROP COLUMN IF EXISTS is_public")
    await conn.execute("ALTER TABLE documents DROP COLUMN IF EXISTS public_slug")
    logger.info("Dropped legacy documents.is_public, documents.public_slug columns")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
