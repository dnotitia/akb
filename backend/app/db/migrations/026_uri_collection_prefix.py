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
    # Idempotency guard. The rewrites below are not safe to re-run after
    # the first pass: subsequent inserts that use the post-rewrite
    # canonical URI shape can collide with the unique constraint on
    # (source_uri, target_uri, relation_type) when we'd rewrite a
    # legacy duplicate onto the canonical row. Skip the whole migration
    # once there are no legacy URI shapes left to rewrite.
    legacy_remaining = await conn.fetchval(
        """
        SELECT 1 FROM (
            SELECT source_uri AS u FROM edges
             WHERE source_uri ~ '^akb://[^/]+/doc/[^/]+/[^/]+'
            UNION ALL
            SELECT target_uri FROM edges
             WHERE target_uri ~ '^akb://[^/]+/doc/[^/]+/[^/]+'
            UNION ALL
            SELECT resource_uri FROM publications
             WHERE resource_uri ~ '^akb://[^/]+/doc/[^/]+/[^/]+'
            UNION ALL
            SELECT resource_uri FROM events
             WHERE resource_uri ~ '^akb://[^/]+/doc/[^/]+/[^/]+'
        ) AS legacy LIMIT 1
        """
    )
    if not legacy_remaining:
        logger.info("Migration 026: no legacy URI shapes remain; skipping")
        return

    async with conn.transaction():
        doc_rewrites = 0
        table_rewrites = 0
        file_rewrites = 0

        # `edges` has UNIQUE (source_uri, target_uri, relation_type). A
        # legacy row whose rewritten form already exists as a canonical
        # twin (or as another legacy row that rewrites to the same key)
        # would trip that constraint on the rewrite UPDATE. This migration
        # re-runs on every boot (no schema_migrations table) and legacy
        # edges can be (re)created by external tools, so the rewrite MUST
        # be conflict-safe: drop colliding legacy edge rows BEFORE the
        # rewrite, preferring to keep the canonical row (or the smaller id
        # among legacy twins). publications/events have no such constraint.

        DOC_WHERE = "~ '^akb://[^/]+/doc/[^/]+/[^/]+'"

        def doc_canon(expr: str) -> str:
            return (
                f"regexp_replace({expr}, "
                f"'^akb://([^/]+)/doc/(.+)/([^/]+)$', "
                f"'akb://\\1/coll/\\2/doc/\\3')"
            )

        # ─── DOC URIs ───────────────────────────────────────────
        #   akb://V/doc/{collection}/{basename} →
        #   akb://V/coll/{collection}/doc/{basename}
        # Root-level docs (no internal slash) don't match and are left as-is.
        for col in ("source_uri", "target_uri"):
            other = "target_uri" if col == "source_uri" else "source_uri"
            # Drop legacy rows that would collide post-rewrite.
            await conn.execute(
                f"""
                DELETE FROM edges l
                 WHERE l.{col} {DOC_WHERE}
                   AND EXISTS (
                     SELECT 1 FROM edges e
                      WHERE e.id <> l.id
                        AND e.relation_type = l.relation_type
                        AND e.{other} = l.{other}
                        AND {doc_canon('e.' + col)} = {doc_canon('l.' + col)}
                        AND (
                          e.{col} !~ '^akb://[^/]+/doc/[^/]+/[^/]+'
                          OR e.id < l.id
                        )
                   )
                """
            )
            result = await conn.execute(
                f"""
                UPDATE edges
                   SET {col} = {doc_canon(col)}
                 WHERE {col} {DOC_WHERE}
                """
            )
            try:
                doc_rewrites += int(result.split()[-1])
            except (ValueError, IndexError):
                pass

        for t, c in (("publications", "resource_uri"), ("events", "resource_uri")):
            result = await conn.execute(
                f"UPDATE {t} SET {c} = {doc_canon(c)} WHERE {c} {DOC_WHERE}"
            )
            try:
                doc_rewrites += int(result.split()[-1])
            except (ValueError, IndexError):
                pass

        # ─── TABLE / FILE URIs ──────────────────────────────────
        # Build per-(vault, resource) legacy→canonical maps, then rewrite
        # rows whose stored URI matches the legacy form. Same conflict-safe
        # dance for edges.
        for kind, tmp, build in (
            (
                "table",
                "_uri_table_rewrite",
                """
                SELECT 'akb://' || v.name || '/table/' || t.name AS legacy_uri,
                       CASE WHEN c.path IS NOT NULL THEN
                         'akb://' || v.name || '/coll/' || c.path || '/table/' || t.name
                       ELSE 'akb://' || v.name || '/table/' || t.name END AS new_uri
                  FROM vault_tables t
                  JOIN vaults v ON v.id = t.vault_id
                  LEFT JOIN collections c ON c.id = t.collection_id
                """,
            ),
            (
                "file",
                "_uri_file_rewrite",
                """
                SELECT 'akb://' || v.name || '/file/' || f.id::text AS legacy_uri,
                       CASE WHEN c.path IS NOT NULL THEN
                         'akb://' || v.name || '/coll/' || c.path || '/file/' || f.id::text
                       ELSE 'akb://' || v.name || '/file/' || f.id::text END AS new_uri
                  FROM vault_files f
                  JOIN vaults v ON v.id = f.vault_id
                  LEFT JOIN collections c ON c.id = f.collection_id
                """,
            ),
        ):
            await conn.execute(f"CREATE TEMP TABLE {tmp} ON COMMIT DROP AS {build}")
            await conn.execute(f"DELETE FROM {tmp} WHERE legacy_uri = new_uri")

            n = 0
            for col in ("source_uri", "target_uri"):
                other = "target_uri" if col == "source_uri" else "source_uri"
                await conn.execute(
                    f"""
                    DELETE FROM edges l USING {tmp} r
                     WHERE l.{col} = r.legacy_uri
                       AND EXISTS (
                         SELECT 1 FROM edges e
                          WHERE e.id <> l.id
                            AND e.relation_type = l.relation_type
                            AND e.{other} = l.{other}
                            AND e.{col} = r.new_uri
                       )
                    """
                )
                result = await conn.execute(
                    f"""
                    UPDATE edges
                       SET {col} = r.new_uri
                      FROM {tmp} r
                     WHERE edges.{col} = r.legacy_uri
                    """
                )
                try:
                    n += int(result.split()[-1])
                except (ValueError, IndexError):
                    pass

            for t, c in (("publications", "resource_uri"), ("events", "resource_uri")):
                result = await conn.execute(
                    f"""
                    UPDATE {t} SET {c} = r.new_uri
                      FROM {tmp} r WHERE {t}.{c} = r.legacy_uri
                    """
                )
                try:
                    n += int(result.split()[-1])
                except (ValueError, IndexError):
                    pass

            if kind == "table":
                table_rewrites += n
            else:
                file_rewrites += n

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
