"""Migration 028: add `edges.kind` to distinguish implicit from explicit edges.

`store_document_relations` rewrites a doc's outgoing edges by
DELETE-then-INSERT on every put/update/edit — the model is "frontmatter
+ body markdown links are the source of truth for this doc's edges."
That model is fine in isolation, but the explicit `akb_link` /
`akb_unlink` API writes into the same `edges` table without any
discriminator, so every routine `akb_update` silently destroys every
explicit edge the user created.

Adding `kind`:

  - 'implicit' (default) — derived from the doc; rewritten by
    store_document_relations on every write.
  - 'explicit' — created by akb_link; persists across doc writes.

After this migration:

  - store_document_relations DELETE is filtered to kind='implicit'.
  - link_resources INSERT sets kind='explicit'.

Existing rows default to 'implicit' (current behaviour), so an
already-deployed instance sees no change in semantics — only future
`akb_link` calls produce durable edges. Re-running `akb_link` for any
edge that was previously erased restores it as explicit.

Idempotent: ADD COLUMN IF NOT EXISTS + DEFAULT 'implicit'. PG 11+ does
this as a metadata-only ALTER (no table rewrite).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.028")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    existing = await conn.fetchval(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'edges'
           AND column_name = 'kind'
        """
    )
    if existing:
        logger.info("Migration 028: edges.kind already present; skipping")
        return

    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE edges
              ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'implicit'
                CHECK (kind IN ('implicit', 'explicit'))
            """
        )
        # Helpful filter for the rewrite DELETE — predicate match.
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_edges_source_kind
                ON edges(source_uri, kind)
            """
        )

    logger.info(
        "Migration 028 added edges.kind (default 'implicit'). "
        "Existing edges are preserved; future akb_link writes mark explicit."
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
