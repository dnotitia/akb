#!/usr/bin/env python3
"""Backfill `vault_id` onto existing pgvector points (issue #189 Phase 2).

Increment A added a `vault_id` column to the vector index (`vector_index.chunks`)
and made the indexing path write it for NEW points. Points indexed BEFORE that
have `vault_id = NULL`. This script populates them — a metadata-only update
(NOT a re-embed): `vault_id` is derived from the already-stored `source_id`
(documents / vault_tables / vault_files all carry vault_id), and the embeddings
are untouched.

Run this ONCE after deploying increment A and BEFORE flipping
`vault_filter_enabled` to True. While any point is still NULL the vault filter
would silently drop it (NULL never matches `= ANY(...)`), which is exactly why
the flag must stay off until `--check` reports 0.

Idempotent: only rows with `vault_id IS NULL` are touched, so re-running (or
resuming after an interrupted run) is safe.

Two deployment shapes:
  - same-instance (`vector_store_dsn` blank): the vector index lives in the main
    DB, so the backfill is a single SERVER-SIDE `UPDATE ... FROM (join)` — zero
    client memory, one transaction.
  - separate-instance: the source→vault map is STREAMED from MAIN via a
    server-side cursor and applied to the VECTOR pool in transactional batches,
    so neither side loads the whole corpus into memory.

Usage:
    python -m scripts.backfill_vault_id            # backfill
    python -m scripts.backfill_vault_id --check    # report NULL count only
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.db.postgres import get_pool, init_db, close_pool
from app.services.vector_store import get_vector_store
from app.services.vector_store.pgvector import PgvectorStore

_BATCH = 5000

# Pulled once; small + static SQL, interpolated only with the configured schema.
_SOURCE_MAP_SQL = """
    SELECT id::text AS sid, vault_id::text AS vid FROM documents
    UNION ALL SELECT id::text, vault_id::text FROM vault_tables
    UNION ALL SELECT id::text, vault_id::text FROM vault_files
"""


async def _vector_pool():
    store = get_vector_store()
    if not isinstance(store, PgvectorStore):
        raise SystemExit(
            f"driver '{settings.vector_store_driver}' has no SQL vault_id column; "
            "this backfill is pgvector-only."
        )
    return await store._pool()


async def _null_count(schema: str) -> int:
    vpool = await _vector_pool()
    async with vpool.acquire() as c:
        return int(await c.fetchval(
            f'SELECT count(*) FROM "{schema}".chunks WHERE vault_id IS NULL'
        ))


async def _apply_batch(vpool, schema: str, batch: list[tuple[str, str]]) -> int:
    sids = [uuid.UUID(s) for s, _ in batch]
    vids = [uuid.UUID(v) for _, v in batch]
    async with vpool.acquire() as vc:
        async with vc.transaction():
            res = await vc.execute(
                f"""
                UPDATE "{schema}".chunks vi
                SET vault_id = m.vid
                FROM (SELECT unnest($1::uuid[]) AS sid,
                             unnest($2::uuid[]) AS vid) m
                WHERE vi.source_id = m.sid AND vi.vault_id IS NULL
                """,
                sids, vids,
            )
    return int(res.split()[-1]) if res.startswith("UPDATE") else 0


async def _backfill_same_instance(schema: str) -> int:
    """Server-side join UPDATE — the vector index is in the main DB, so no rows
    cross into Python. Single transaction; idempotent on `vault_id IS NULL`."""
    pool = await get_pool()
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute(
                f"""
                UPDATE "{schema}".chunks vi
                SET vault_id = src.vault_id
                FROM (
                  SELECT id, vault_id FROM documents
                  UNION ALL SELECT id, vault_id FROM vault_tables
                  UNION ALL SELECT id, vault_id FROM vault_files
                ) src
                WHERE vi.source_id = src.id AND vi.vault_id IS NULL
                """
            )
    return int(res.split()[-1]) if res.startswith("UPDATE") else 0


async def _backfill_separate(schema: str) -> int:
    """Separate vector instance: stream the source→vault map from MAIN via a
    server-side cursor and apply it to the vector pool in transactional batches.
    Bounded memory on both sides."""
    main = await get_pool()
    vpool = await _vector_pool()
    updated = 0
    async with main.acquire() as mc:
        async with mc.transaction():  # asyncpg cursors require a transaction
            batch: list[tuple[str, str]] = []
            async for r in mc.cursor(_SOURCE_MAP_SQL, prefetch=_BATCH):
                batch.append((r["sid"], r["vid"]))
                if len(batch) >= _BATCH:
                    updated += await _apply_batch(vpool, schema, batch)
                    batch = []
            if batch:
                updated += await _apply_batch(vpool, schema, batch)
    return updated


async def _log_orphans(schema: str) -> None:
    """When points remain NULL, surface a sample of the offending source_ids so
    an operator can investigate (stale vector rows whose source row was deleted),
    rather than only a count."""
    vpool = await _vector_pool()
    async with vpool.acquire() as c:
        rows = await c.fetch(
            f"""
            SELECT DISTINCT source_id::text AS sid
            FROM "{schema}".chunks WHERE vault_id IS NULL LIMIT 20
            """
        )
    if rows:
        print("  orphan source_id sample (up to 20): "
              + ", ".join(r["sid"] for r in rows))


async def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill vault_id on pgvector points (#189 Phase 2).")
    ap.add_argument("--check", action="store_true",
                    help="report how many points still have NULL vault_id, then exit")
    args = ap.parse_args()

    await init_db()
    schema = settings.vector_store_schema
    try:
        if args.check:
            n = await _null_count(schema)
            print(f"{n} point(s) with vault_id IS NULL in {schema}.chunks")
            return

        same_instance = not settings.vector_store_dsn.strip()
        if same_instance:
            updated = await _backfill_same_instance(schema)
        else:
            updated = await _backfill_separate(schema)
        remaining = await _null_count(schema)

        if remaining == 0:
            print(f"backfilled {updated} point(s); 0 still NULL "
                  "(OK — safe to flip vault_filter_enabled).")
        else:
            print(f"backfilled {updated} point(s); {remaining} still NULL — "
                  "these are orphans (source_id not in any resource table). "
                  "Do NOT flip vault_filter_enabled until 0.")
            await _log_orphans(schema)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
