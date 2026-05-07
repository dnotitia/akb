"""Migration 011: backfill `documents.collection_id` for external_git docs.

The initial external_git ingestion shipped with `collection_id = NULL`,
which made `akb_browse` return empty for mirror vaults. The reindex
code was fixed to derive `collection_id` from the external_path's
parent directory, but rows already written by the old code need a
one-time backfill — this migration performs it idempotently.

On fresh DBs this is a no-op (no external_git rows yet, or all rows
already have collection_id set).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.011")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # Fetch all external_git docs currently missing a collection_id.
    rows = await conn.fetch(
        """
        SELECT id, vault_id, external_path
          FROM documents
         WHERE source = 'external_git'
           AND collection_id IS NULL
           AND external_path IS NOT NULL
        """
    )
    if not rows:
        logger.info("Migration 011: no external_git docs need collection_id backfill")
        return

    # Group docs by (vault_id, parent_dir). Skip docs at the repo root —
    # those legitimately have no parent (collection_id stays NULL).
    buckets: dict[tuple[uuid.UUID, str], list[uuid.UUID]] = {}
    for r in rows:
        parent = str(PurePosixPath(r["external_path"]).parent)
        if parent in ("", "."):
            continue
        buckets.setdefault((r["vault_id"], parent), []).append(r["id"])

    backfilled = 0
    for (vault_id, coll_path), doc_ids in buckets.items():
        existing = await conn.fetchval(
            "SELECT id FROM collections WHERE vault_id = $1 AND path = $2",
            vault_id, coll_path,
        )
        if existing is None:
            coll_id = uuid.uuid4()
            name = coll_path.rstrip("/").split("/")[-1]
            await conn.execute(
                "INSERT INTO collections (id, vault_id, path, name) VALUES ($1, $2, $3, $4)",
                coll_id, vault_id, coll_path, name,
            )
        else:
            coll_id = existing
        await conn.execute(
            "UPDATE documents SET collection_id = $1 WHERE id = ANY($2::uuid[])",
            coll_id, doc_ids,
        )
        backfilled += len(doc_ids)

    logger.info(
        "Migration 011: backfilled collection_id on %d external_git docs across %d collections",
        backfilled, len(buckets),
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
