"""Migration 021: collapse (events.ref_type, events.ref_id) → events.resource_uri.

Before: every event row carried two columns identifying the resource —
        `ref_type ∈ {document, table, file, collection, user}` plus
        `ref_id` (a d-prefix id, UUID, or path string depending on type).
        External Redis-stream consumers had to know that mapping to make
        sense of an event.

After:  a single `resource_uri` column holds the canonical akb:// URI
        (or NULL for events that don't reference an in-vault resource —
        e.g. user / collection / vault-level events). Stream consumers
        see exactly what MCP clients see.

Migration logic:

1. ALTER TABLE events ADD COLUMN resource_uri TEXT (idempotent).
2. Backfill resource_uri from existing rows per ref_type:
     - document: JOIN documents on metadata->>'id' = ref_id → use doc.path
     - table:    JOIN vault_tables on id::text = ref_id → use t.name
     - file:     ref_id IS the UUID → use it directly
     - collection / user / anything else → leave resource_uri NULL
3. Verify: every row whose ref_type was document/table/file either has
   a populated resource_uri or its ref_id has no matching row (orphan
   history). Log the orphan count, do not fail — these are historical
   rows downstream consumers haven't been able to address either.
4. DROP COLUMN ref_type.
5. DROP COLUMN ref_id.
6. CREATE INDEX idx_events_resource_uri (partial, WHERE resource_uri
   IS NOT NULL — keeps the index small since auth/collection events
   are URI-less).

Idempotent: re-running after step 6 finds ref_type/ref_id absent and
resource_uri present; `_already_applied` returns True and skips.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.021")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _column_exists(conn, table: str, column: str) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = $1
           AND column_name = $2
        """,
        table, column,
    ))


async def _already_applied(conn) -> bool:
    has_uri = await _column_exists(conn, "events", "resource_uri")
    has_ref_type = await _column_exists(conn, "events", "ref_type")
    has_ref_id = await _column_exists(conn, "events", "ref_id")
    return bool(has_uri and not has_ref_type and not has_ref_id)


async def _run(conn):
    if await _already_applied(conn):
        logger.info(
            "Migration 021 already applied "
            "(resource_uri present, ref_type/ref_id removed); skipping"
        )
        return

    async with conn.transaction():
        # Step 1: add resource_uri (idempotent).
        await conn.execute(
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS resource_uri TEXT"
        )

        ref_type_present = await _column_exists(conn, "events", "ref_type")
        ref_id_present = await _column_exists(conn, "events", "ref_id")

        if ref_type_present and ref_id_present:
            # Step 2a: documents — join on metadata->>'id' (the d-prefix
            # id stored when the doc was first put), use documents.path
            # for the URI.
            doc_count = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE events e
                       SET resource_uri =
                           'akb://' || v.name || '/doc/' || d.path
                      FROM vaults v, documents d
                     WHERE e.ref_type = 'document'
                       AND e.resource_uri IS NULL
                       AND v.id = e.vault_id
                       AND d.vault_id = e.vault_id
                       AND d.metadata->>'id' = e.ref_id
                     RETURNING e.id
                )
                SELECT COUNT(*) FROM updated
                """
            )

            # Step 2b: tables — ref_id is the vault_tables.id UUID;
            # URI uses vault_tables.name.
            table_count = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE events e
                       SET resource_uri =
                           'akb://' || v.name || '/table/' || t.name
                      FROM vaults v, vault_tables t
                     WHERE e.ref_type = 'table'
                       AND e.resource_uri IS NULL
                       AND v.id = e.vault_id
                       AND t.id::text = e.ref_id
                     RETURNING e.id
                )
                SELECT COUNT(*) FROM updated
                """
            )

            # Step 2c: files — ref_id is the vault_files.id UUID;
            # URI is built directly from it (no name lookup needed).
            file_count = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE events e
                       SET resource_uri =
                           'akb://' || v.name || '/file/' || e.ref_id
                      FROM vaults v
                     WHERE e.ref_type = 'file'
                       AND e.resource_uri IS NULL
                       AND v.id = e.vault_id
                     RETURNING e.id
                )
                SELECT COUNT(*) FROM updated
                """
            )

            # Step 3: count orphan rows (doc/table whose ref_id no
            # longer joins) — they remain NULL after backfill.
            orphan_doc = await conn.fetchval(
                """
                SELECT COUNT(*) FROM events
                 WHERE ref_type = 'document' AND resource_uri IS NULL
                """
            )
            orphan_table = await conn.fetchval(
                """
                SELECT COUNT(*) FROM events
                 WHERE ref_type = 'table' AND resource_uri IS NULL
                """
            )
            url_less = await conn.fetchval(
                """
                SELECT COUNT(*) FROM events
                 WHERE ref_type IS NOT NULL
                   AND ref_type NOT IN ('document', 'table', 'file')
                """
            )

            logger.info(
                "Migration 021 backfilled events.resource_uri: "
                "%d documents, %d tables, %d files (orphans: %d doc / %d table; "
                "%d non-resource events left URI-less by design)",
                doc_count or 0, table_count or 0, file_count or 0,
                orphan_doc or 0, orphan_table or 0, url_less or 0,
            )

            # Step 4 + 5: drop the legacy columns.
            await conn.execute("ALTER TABLE events DROP COLUMN ref_type")
            await conn.execute("ALTER TABLE events DROP COLUMN ref_id")

        # Step 6: partial index on the new column. Keeps the index
        # small because most auth / collection events leave the column
        # NULL by design.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_resource_uri "
            "ON events (resource_uri) "
            "WHERE resource_uri IS NOT NULL"
        )

        logger.info(
            "Migration 021 applied: events.resource_uri replaces "
            "(ref_type, ref_id); index created"
        )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
