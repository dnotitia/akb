"""Migration 027: warn about collection paths whose segments collide
with URI structural markers.

The 0.3.0 URI grammar is ``akb://V/coll/<coll_path>/<type>/<id>``
with ``<type>`` in ``{doc, table, file}``. A collection whose path
contains ``coll`` / ``doc`` / ``table`` / ``file`` as a segment
(not at the leaf — leaves are safe) can produce a URI that
``parse_uri`` mis-classifies as a typed-resource URI:

    coll path "frontend/file/X"
      → coll URI  "akb://V/coll/frontend/file/X"
      → parsed as file URI (coll="frontend", id="X")

Going forward the input layer (``normalize_collection_path``)
refuses these segments at create time. This migration scans the
existing ``collections`` table for any pre-0.3.0 row that already
has the issue, logs them, and lets the operator decide whether to
rename — we do **not** auto-mutate paths because that would also
need a cascading rewrite of every URI that references them
(documents.path, edges, publications, events, …) which is too
risky for a passive startup migration.

Reads the table, never writes — strictly informational. Idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db
from app.util.text import RESERVED_COLLECTION_SEGMENTS

logger = logging.getLogger("akb.migration.027")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # Pull every collection path; client-side segment check keeps the
    # SQL simple and works regardless of which reserved words we
    # decide to add in the future. The collections table is small
    # (one row per logical folder; usually < 100 per vault), so the
    # full scan cost is negligible.
    rows = await conn.fetch(
        """
        SELECT c.id, c.path, v.name AS vault_name
          FROM collections c
          JOIN vaults v ON v.id = c.vault_id
         ORDER BY v.name, c.path
        """
    )

    offending: list[tuple[str, str]] = []
    for r in rows:
        path = r["path"] or ""
        if not path:
            continue
        segments = path.split("/")
        if any(seg in RESERVED_COLLECTION_SEGMENTS for seg in segments):
            offending.append((r["vault_name"], path))

    if not offending:
        logger.info(
            "Migration 027: scanned %d collection rows; no segments collide "
            "with URI structural markers (%s). New input is also rejected "
            "by normalize_collection_path.",
            len(rows), sorted(RESERVED_COLLECTION_SEGMENTS),
        )
        return

    logger.warning(
        "Migration 027: found %d collection(s) with reserved-word segments. "
        "URIs for these may round-trip ambiguously — rename via "
        "akb_create_collection (new name) + manual move, or accept the "
        "navigation quirk. Affected:",
        len(offending),
    )
    for vault, path in offending:
        logger.warning("  vault=%s  path=%s", vault, path)


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
