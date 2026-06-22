"""Migration 039: composite (vault_id, endpoint) indexes on `edges`.

Every graph read scopes by vault AND filters/joins on an endpoint URI:

  - BFS (`_bfs_collect`):   WHERE source_uri = ANY($1) AND vault_id = $2
                            WHERE target_uri = ANY($1) AND vault_id = $2
  - overview induced edges: WHERE vault_id = $1
                              AND source_uri = ANY($2) AND target_uri = ANY($2)
  - degree/health rollups:  GROUP BY over (vault_id-scoped) source/target.

The pre-existing single-column indexes (`idx_edges_vault`,
`idx_edges_source`, `idx_edges_target`) force the planner to pick ONE and
re-check the other predicate per row. A composite `(vault_id, source_uri)` /
`(vault_id, target_uri)` lets a single index satisfy both the vault scope and
the endpoint lookup — the common access pattern for every graph query — so the
ANY(...) probes stay index-only as a vault's edge count grows.

Idempotent: `CREATE INDEX IF NOT EXISTS`. Non-CONCURRENT (runs inside the
migration transaction like 028's `idx_edges_source_kind`); the brief
ACCESS-SHARE-blocking build is acceptable at migration time. Leaves the
existing single-column indexes in place — they still serve cross-vault
admin/debug probes and the planner drops to them when a composite isn't a fit.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.039")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_edges_vault_source
                ON edges(vault_id, source_uri)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_edges_vault_target
                ON edges(vault_id, target_uri)
            """
        )

    logger.info(
        "Migration 039 added composite edge indexes "
        "idx_edges_vault_source(vault_id, source_uri) + "
        "idx_edges_vault_target(vault_id, target_uri)."
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
