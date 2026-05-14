"""Migration 020: unified collection membership for documents / tables / files.

Before:  `documents.collection_id` FK to collections (since migration 011).
         `vault_files.collection` is a free-form TEXT field — never normalized
         against `collections`. `vault_tables` has no collection concept.

After:   All three resource types reference `collections.id` via a
         nullable `collection_id` FK (NULL == vault root). The legacy
         `vault_files.collection` TEXT column is removed in the same
         migration — no two-source-of-truth window.

Migration logic:

1.  ALTER TABLE vault_tables ADD COLUMN collection_id UUID
       REFERENCES collections(id) ON DELETE SET NULL.
2.  ALTER TABLE vault_files  ADD COLUMN collection_id UUID
       REFERENCES collections(id) ON DELETE SET NULL.
3.  For every vault_files row whose `collection` text is non-empty,
    ensure a matching `collections` row exists (insert one if not),
    then set vault_files.collection_id to it.
    Empty / NULL text → leave collection_id NULL (= vault root).
4.  Verify: every row's collection state is consistent — either both
    NULL (root) or both NOT NULL with the FK target's path matching
    the original text. Abort the migration on mismatch.
5.  DROP COLUMN vault_files.collection.

Idempotent on partial application — re-running after step 5 finds the
column missing and the FK columns present; the `_already_applied`
check below catches that and exits cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.020")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _already_applied(conn) -> bool:
    """Returns True if the post-migration column shape is already in place
    (collection_id on both tables AND legacy TEXT column gone)."""
    has_tables_fk = await conn.fetchval(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'vault_tables'
           AND column_name = 'collection_id'
        """
    )
    has_files_fk = await conn.fetchval(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'vault_files'
           AND column_name = 'collection_id'
        """
    )
    legacy_text_gone = await conn.fetchval(
        """
        SELECT NOT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'vault_files'
               AND column_name = 'collection'
        )
        """
    )
    return bool(has_tables_fk and has_files_fk and legacy_text_gone)


async def _run(conn):
    if await _already_applied(conn):
        logger.info(
            "Migration 020 already applied "
            "(collection_id FKs present, legacy TEXT removed); skipping"
        )
        return

    async with conn.transaction():
        # 1+2: add FK columns (idempotent via IF NOT EXISTS).
        await conn.execute(
            """
            ALTER TABLE vault_tables
                ADD COLUMN IF NOT EXISTS collection_id UUID
                    REFERENCES collections(id) ON DELETE SET NULL
            """
        )
        await conn.execute(
            """
            ALTER TABLE vault_files
                ADD COLUMN IF NOT EXISTS collection_id UUID
                    REFERENCES collections(id) ON DELETE SET NULL
            """
        )

        # 3: populate vault_files.collection_id from existing TEXT.
        # Only meaningful if the legacy column still exists — guard so
        # the migration is safe to re-run on a partially-applied DB
        # (FKs added but TEXT not yet dropped).
        text_col_exists = await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'vault_files'
               AND column_name = 'collection'
            """
        )
        migrated = 0
        if text_col_exists:
            distinct_pairs = await conn.fetch(
                """
                SELECT DISTINCT vault_id, collection
                  FROM vault_files
                 WHERE collection IS NOT NULL
                   AND collection <> ''
                """
            )
            for row in distinct_pairs:
                vault_id = row["vault_id"]
                path = row["collection"]
                # ensure_collection: insert if missing.
                cid_row = await conn.fetchrow(
                    "SELECT id FROM collections WHERE vault_id = $1 AND path = $2",
                    vault_id, path,
                )
                if cid_row:
                    cid = cid_row["id"]
                else:
                    cid = uuid.uuid4()
                    name = path.rstrip("/").split("/")[-1] or path
                    await conn.execute(
                        """
                        INSERT INTO collections (id, vault_id, path, name)
                        VALUES ($1, $2, $3, $4)
                        """,
                        cid, vault_id, path, name,
                    )
                    logger.info(
                        "Migration 020: ensured collection (%s, %s) for file backfill",
                        vault_id, path,
                    )
                # Set FK on every vault_files row matching this (vault, path).
                result = await conn.execute(
                    """
                    UPDATE vault_files
                       SET collection_id = $1
                     WHERE vault_id = $2
                       AND collection = $3
                       AND collection_id IS NULL
                    """,
                    cid, vault_id, path,
                )
                # asyncpg returns "UPDATE N" — extract N for the audit log.
                try:
                    migrated += int(result.split()[-1])
                except (ValueError, IndexError):
                    pass

            # 4: verify — every row with non-empty TEXT collection now has
            # a matching FK whose path equals the original text.
            inconsistent = await conn.fetchval(
                """
                SELECT COUNT(*) FROM vault_files vf
                  LEFT JOIN collections c ON c.id = vf.collection_id
                 WHERE vf.collection IS NOT NULL
                   AND vf.collection <> ''
                   AND (
                        vf.collection_id IS NULL
                     OR c.path IS DISTINCT FROM vf.collection
                     OR c.vault_id IS DISTINCT FROM vf.vault_id
                   )
                """
            )
            if int(inconsistent or 0) > 0:
                raise RuntimeError(
                    f"Migration 020: {inconsistent} vault_files rows have a non-empty "
                    "legacy `collection` text that did not migrate cleanly to "
                    "`collection_id`. Aborting before dropping the legacy column."
                )

            logger.info(
                "Migration 020: vault_files.collection → collection_id backfilled "
                "for %d rows", migrated,
            )

            # 5: drop legacy TEXT column.
            await conn.execute("ALTER TABLE vault_files DROP COLUMN collection")

        logger.info(
            "Migration 020 applied: vault_tables.collection_id + vault_files.collection_id "
            "FK in place; legacy vault_files.collection TEXT removed",
        )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
