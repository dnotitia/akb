"""Migration 035: repair edges whose target_uri carries a wikilink alias.

Body-link extraction (`kg_service.extract_markdown_links`) had no handling
for Obsidian wikilinks `[[target|alias]]`. The greedy bare-`akb://` scan
then swallowed the alias's first word onto the target — a References line
like::

    [[akb://v/coll/decisions/doc/x.md|PWC Query Performance Optimization]]

produced the edge target ``akb://v/coll/decisions/doc/x.md|PWC`` (matching
stopped at the first space). That target matches no document node, so the
graph drew no edge and the relations panel rendered a ``…%7CPWC`` broken
link. The parser + `_store_edge` existence check are fixed going forward;
this migration repairs the rows already persisted.

For each edge whose ``target_uri`` contains ``|``:
  1. cleaned = everything before the first ``|`` (the real target).
  2. If a resource exists at the cleaned URI → insert the corrected edge
     (ON CONFLICT DO NOTHING, so an already-correct twin is kept) and drop
     the corrupted row.
  3. Otherwise (cleaned target doesn't resolve) → just drop the corrupted
     row; it was an orphan that could never be drawn.

Uses the SAME `parse_uri` / `_resource_exists` the runtime path uses, so a
repaired edge is identical to one re-extracted from the body.

Idempotent: after one pass no ``target_uri`` contains ``|`` (canonical
akb:// URIs never do), so a re-run finds nothing.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db
from app.services.kg_service import _resource_exists
from app.services.uri_service import parse_uri

logger = logging.getLogger("akb.migration.035")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    rows = await conn.fetch(
        """
        SELECT id, vault_id, source_uri, target_uri, relation_type,
               source_type, target_type, kind
          FROM edges
         WHERE target_uri LIKE '%|%'
        """
    )
    if not rows:
        logger.info("Migration 035: no alias-corrupted edges; skipping")
        return

    repaired = 0
    dropped = 0
    async with conn.transaction():
        for r in rows:
            cleaned = r["target_uri"].split("|", 1)[0]
            keep = False
            parsed = parse_uri(cleaned)
            if parsed and parsed.kind in ("doc", "table", "file"):
                target_vault_id = await conn.fetchval(
                    "SELECT id FROM vaults WHERE name = $1", parsed.vault,
                )
                if target_vault_id is not None and await _resource_exists(
                    conn, target_vault_id, parsed.kind, parsed.identifier or "",
                ):
                    keep = True

            # Always remove the corrupted row.
            await conn.execute("DELETE FROM edges WHERE id = $1", r["id"])

            if keep:
                # Re-insert the corrected edge; ON CONFLICT keeps an existing
                # correct twin (and preserves its kind via DO NOTHING).
                await conn.execute(
                    """
                    INSERT INTO edges (id, vault_id, source_uri, target_uri,
                                       relation_type, source_type, target_type, kind)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT DO NOTHING
                    """,
                    uuid.uuid4(), r["vault_id"], r["source_uri"], cleaned,
                    r["relation_type"], r["source_type"], r["target_type"], r["kind"],
                )
                repaired += 1
            else:
                dropped += 1

    logger.info(
        "Migration 035: repaired %d alias-corrupted edge(s), "
        "dropped %d orphan(s) (cleaned target did not resolve)",
        repaired, dropped,
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
