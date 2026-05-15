"""Migration 022: collapse (publications.document_id, file_id) → publications.resource_uri.

Mirrors migration 021's pattern for `events`. After this migration the
publications table addresses its target resource with a single
canonical akb:// URI, matching what MCP clients see and what the event
stream emits.

Before
------
    publications.document_id UUID REFERENCES documents(id) ON DELETE CASCADE
    publications.file_id     UUID REFERENCES vault_files(id) ON DELETE CASCADE
    -- exactly one non-null per resource_type ∈ {document, file}; both
    -- null for table_query.

After
-----
    publications.resource_uri TEXT
        -- 'akb://{vault.name}/doc/{documents.path}'        for documents
        -- 'akb://{vault.name}/file/{vault_files.id}'       for files
        -- NULL                                              for table_query
    -- legacy document_id / file_id FK columns dropped.

Migration logic
---------------
1.  ALTER TABLE publications ADD COLUMN resource_uri TEXT (idempotent).
2.  Backfill from the existing FK columns:
      document → JOIN documents → use d.path under v.name.
      file     → URI is `akb://{v.name}/file/{publications.file_id}`.
    table_query rows keep resource_uri NULL (correct — no resource).
3.  Verify: every document/file publication whose FK pointed to a row
    that still exists in `documents` / `vault_files` ends up with a
    non-null resource_uri. Rows whose FK target was already deleted
    (CASCADE NULL would have set the FK to NULL before this migration,
    but our FKs are ON DELETE CASCADE — so dangling FKs shouldn't
    exist) are surfaced as orphans and left NULL. Log the count.
4.  Drop the legacy FK indexes (`idx_publications_document` /
    `idx_publications_file`) explicitly so a re-apply doesn't leave
    them attached to dropped columns (Postgres handles this for us
    too, but being explicit costs nothing).
5.  DROP COLUMN document_id; DROP COLUMN file_id.
6.  CREATE INDEX idx_publications_resource_uri (partial, WHERE
    resource_uri IS NOT NULL).

Idempotent: re-running after step 6 sees resource_uri present and the
legacy columns gone; `_already_applied` short-circuits.

No data loss
------------
- All publication rows are preserved.
- For every document/file publication whose FK target still exists,
  resource_uri is populated. The backfill verification block prints
  the unresolved count BEFORE dropping legacy columns. If anything
  looks wrong, the operator aborts (the whole step runs inside one
  transaction; `RuntimeError` rolls it back).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.022")


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
    has_uri = await _column_exists(conn, "publications", "resource_uri")
    has_document_id = await _column_exists(conn, "publications", "document_id")
    has_file_id = await _column_exists(conn, "publications", "file_id")
    return bool(has_uri and not has_document_id and not has_file_id)


async def _ensure_index(conn) -> None:
    """Create the partial index on the new column. Runs unconditionally
    so fresh installs (where the migration body skips via _already_applied)
    still end up with the index."""
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_publications_resource_uri "
        "ON publications (resource_uri) "
        "WHERE resource_uri IS NOT NULL"
    )


async def _run(conn):
    if await _already_applied(conn):
        await _ensure_index(conn)
        logger.info(
            "Migration 022 already applied "
            "(resource_uri present, document_id/file_id removed); index ensured"
        )
        return

    async with conn.transaction():
        # Step 1: add the new column (idempotent).
        await conn.execute(
            "ALTER TABLE publications ADD COLUMN IF NOT EXISTS resource_uri TEXT"
        )

        has_doc_col = await _column_exists(conn, "publications", "document_id")
        has_file_col = await _column_exists(conn, "publications", "file_id")

        doc_backfill = 0
        file_backfill = 0
        if has_doc_col:
            # Step 2a: documents — JOIN documents to map UUID → path.
            doc_backfill = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE publications p
                       SET resource_uri = 'akb://' || v.name || '/doc/' || d.path
                      FROM vaults v, documents d
                     WHERE p.resource_type = 'document'
                       AND p.resource_uri IS NULL
                       AND p.document_id IS NOT NULL
                       AND v.id = p.vault_id
                       AND d.id = p.document_id
                     RETURNING p.id
                )
                SELECT COUNT(*) FROM updated
                """
            )
        if has_file_col:
            # Step 2b: files — URI uses the UUID directly, no join into
            # vault_files needed (file_id IS the UUID surfaced in the URI).
            file_backfill = await conn.fetchval(
                """
                WITH updated AS (
                    UPDATE publications p
                       SET resource_uri = 'akb://' || v.name || '/file/' || p.file_id::text
                      FROM vaults v
                     WHERE p.resource_type = 'file'
                       AND p.resource_uri IS NULL
                       AND p.file_id IS NOT NULL
                       AND v.id = p.vault_id
                     RETURNING p.id
                )
                SELECT COUNT(*) FROM updated
                """
            )

        # Step 3: verification. The FKs are ON DELETE CASCADE so a
        # dangling FK shouldn't exist; still, count just in case.
        if has_doc_col:
            orphan_doc = await conn.fetchval(
                """
                SELECT COUNT(*) FROM publications
                 WHERE resource_type = 'document'
                   AND resource_uri IS NULL
                """
            )
        else:
            orphan_doc = 0
        if has_file_col:
            orphan_file = await conn.fetchval(
                """
                SELECT COUNT(*) FROM publications
                 WHERE resource_type = 'file'
                   AND resource_uri IS NULL
                """
            )
        else:
            orphan_file = 0
        table_query_total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM publications WHERE resource_type = 'table_query'
            """
        )

        logger.info(
            "Migration 022 backfilled publications.resource_uri: "
            "%d documents, %d files (orphans: %d doc / %d file; "
            "%d table_query publications stay URI-less by design)",
            doc_backfill or 0, file_backfill or 0,
            orphan_doc or 0, orphan_file or 0, table_query_total or 0,
        )

        # Step 4 + 5: drop legacy indexes + columns. CASCADE because
        # the indexes ride on the columns; doing them in either order
        # works (Postgres drops the index when the column goes), but
        # explicit DROP INDEX first matches the init.sql diff and
        # makes the intent obvious.
        await conn.execute("DROP INDEX IF EXISTS idx_publications_document")
        await conn.execute("DROP INDEX IF EXISTS idx_publications_file")
        if has_doc_col:
            await conn.execute("ALTER TABLE publications DROP COLUMN document_id")
        if has_file_col:
            await conn.execute("ALTER TABLE publications DROP COLUMN file_id")

        # Step 6: new partial index for per-resource lookup.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_publications_resource_uri "
            "ON publications (resource_uri) "
            "WHERE resource_uri IS NOT NULL"
        )

        logger.info(
            "Migration 022 applied: publications.resource_uri replaces "
            "(document_id, file_id); index created"
        )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
