"""Migration 026: rewrite stored URIs to the 0.3.0 location-aware canonical form.

Pre-0.3.0 the URI scheme placed the collection inside the doc path
itself (``akb://V/doc/specs/api.md``) but emitted table and file URIs
with no collection prefix at all (``akb://V/table/expenses``,
``akb://V/file/<uuid>``). 0.3.0 unifies the scheme — every URI carries
an optional ``/coll/<path>`` segment that names its containing
collection — so siblings of any resource are discoverable by walking
up the URI and pasting it back into ``akb_browse``.

This migration rewrites every URI persisted in:

  - ``edges``        (``source_uri`` and ``target_uri``)
  - ``publications`` (``resource_uri``)
  - ``events``       (``resource_uri``)

…to the new canonical form. The DB schema itself does not change —
columns and indexes are untouched. Idempotent: a row already in
canonical form is matched by the regexes below as a no-op rewrite,
and an empty/NULL URI is skipped.

Transformations (all per-vault, vault name comes from `vaults.name`):

  doc        ``akb://V/doc/<path>``
             ``<path>`` already encodes the collection. The split is
             at the LAST slash — parent = collection, basename = file.
             Implemented as a pure SQL regexp_replace; no JOIN needed
             beyond the URI itself.

  table      ``akb://V/table/<name>``  → JOIN vault_tables → collections
             Add ``/coll/<collection.path>`` between vault and ``/table``
             when the table sits inside a collection. NULL collection
             ⇒ URI stays root-level.

  file       ``akb://V/file/<uuid>``   → JOIN vault_files → collections
             Same shape as table.

Frontmatter URIs inside markdown bodies (``depends_on`` /
``related_to`` arrays) are NOT rewritten by this migration — those
would require rewriting + committing thousands of doc bodies through
git. Old URIs in frontmatter become unparseable as of 0.3.0; edge
extraction logs a warning when it encounters one. An optional batch-
rewrite tool can be run later if needed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.026")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        doc_rewrites = 0
        table_rewrites = 0
        file_rewrites = 0

        # ─── DOC URIs ───────────────────────────────────────────
        # Pure regex rewrite. Pattern: split on the LAST slash inside
        # the path. PostgreSQL regex group references in
        # regexp_replace use ``\N`` style.
        #
        #   akb://V/doc/{collection}/{basename}   →
        #   akb://V/coll/{collection}/doc/{basename}
        #
        # If the path has no internal slash (root-level doc) the
        # pattern doesn't match and the URI is left as-is.
        for table_col in (
            ("edges", "source_uri"),
            ("edges", "target_uri"),
            ("publications", "resource_uri"),
            ("events", "resource_uri"),
        ):
            t, c = table_col
            result = await conn.execute(
                f"""
                UPDATE {t}
                   SET {c} = regexp_replace(
                         {c},
                         '^akb://([^/]+)/doc/(.+)/([^/]+)$',
                         'akb://\\1/coll/\\2/doc/\\3'
                       )
                 WHERE {c} ~ '^akb://[^/]+/doc/[^/]+/[^/]+'
                """
            )
            try:
                doc_rewrites += int(result.split()[-1])
            except (ValueError, IndexError):
                pass

        # ─── TABLE URIs ─────────────────────────────────────────
        # Build the canonical URI per (vault, table) pair, then UPDATE
        # rows whose stored URI matches the legacy form.
        await conn.execute(
            """
            CREATE TEMP TABLE _uri_table_rewrite ON COMMIT DROP AS
            SELECT
              'akb://' || v.name || '/table/' || t.name AS legacy_uri,
              CASE WHEN c.path IS NOT NULL THEN
                'akb://' || v.name || '/coll/' || c.path || '/table/' || t.name
              ELSE
                'akb://' || v.name || '/table/' || t.name
              END AS new_uri
              FROM vault_tables t
              JOIN vaults v ON v.id = t.vault_id
              LEFT JOIN collections c ON c.id = t.collection_id
            """
        )
        # Skip no-op rewrites (root-level table — legacy already canonical)
        await conn.execute("DELETE FROM _uri_table_rewrite WHERE legacy_uri = new_uri")

        for t, c in (
            ("edges", "source_uri"),
            ("edges", "target_uri"),
            ("publications", "resource_uri"),
            ("events", "resource_uri"),
        ):
            result = await conn.execute(
                f"""
                UPDATE {t}
                   SET {c} = r.new_uri
                  FROM _uri_table_rewrite r
                 WHERE {t}.{c} = r.legacy_uri
                """
            )
            try:
                table_rewrites += int(result.split()[-1])
            except (ValueError, IndexError):
                pass

        # ─── FILE URIs ──────────────────────────────────────────
        await conn.execute(
            """
            CREATE TEMP TABLE _uri_file_rewrite ON COMMIT DROP AS
            SELECT
              'akb://' || v.name || '/file/' || f.id::text AS legacy_uri,
              CASE WHEN c.path IS NOT NULL THEN
                'akb://' || v.name || '/coll/' || c.path || '/file/' || f.id::text
              ELSE
                'akb://' || v.name || '/file/' || f.id::text
              END AS new_uri
              FROM vault_files f
              JOIN vaults v ON v.id = f.vault_id
              LEFT JOIN collections c ON c.id = f.collection_id
            """
        )
        await conn.execute("DELETE FROM _uri_file_rewrite WHERE legacy_uri = new_uri")

        for t, c in (
            ("edges", "source_uri"),
            ("edges", "target_uri"),
            ("publications", "resource_uri"),
            ("events", "resource_uri"),
        ):
            result = await conn.execute(
                f"""
                UPDATE {t}
                   SET {c} = r.new_uri
                  FROM _uri_file_rewrite r
                 WHERE {t}.{c} = r.legacy_uri
                """
            )
            try:
                file_rewrites += int(result.split()[-1])
            except (ValueError, IndexError):
                pass

    if doc_rewrites or table_rewrites or file_rewrites:
        logger.info(
            "Migration 026: rewrote URIs to 0.3.0 canonical form "
            "(docs=%d, tables=%d, files=%d).",
            doc_rewrites, table_rewrites, file_rewrites,
        )
    else:
        logger.info(
            "Migration 026: no legacy URIs found; nothing to rewrite."
        )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
