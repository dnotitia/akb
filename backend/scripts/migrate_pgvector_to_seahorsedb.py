"""Bulk migrate already-indexed chunks from pgvector to seahorse-db
without re-calling the embedding API.

Why this exists
---------------
The default driver-switch path is to flip ``vector_store_driver`` in
``app.yaml`` and let ``embed_worker`` rebuild the entire vector store
from scratch — for every chunk that means a fresh ``generate_embeddings``
round trip plus a Coral upsert. On a 35k-chunks production vault that's
~$5-10 of OpenRouter cost and ~30-60 min of wall clock, plus the
sustained-load tail that exposes any Coral retry behaviour.

The dense vectors are already sitting in pgvector's ``vector_index.chunks``
table, deterministic from ``(content, model)``. This script reads them
directly and ships them to a fresh seahorse-db table — embedding cost
zero, total cost = bulk JSONL POSTs + Kafka throughput.

Sparse is re-encoded from ``chunks.content`` via ``sparse_encoder``
because seahorse-db uses a different weight convention than pgvector
(raw TF + query weight 1, vs pgvector's pre-saturated TF + IDF — see
``sparse_encoder`` module docstring). Re-encoding is cheap; the
Kiwi tokenizer caches its results.

Caveats
-------
- Run against a stopped backend, or accept that a concurrently running
  ``embed_worker`` will race this script for the seahorse-db table.
- The seahorse-db driver's ``ensure_collection`` (called once at script
  start) handles table creation idempotently. Re-running this script
  against a partially-migrated table is safe: ``upsert_one`` is
  idempotent on (table_name, id).
- This script does NOT touch the source pgvector index. After a
  successful migration the operator flips ``vector_store_driver`` and
  restarts the backend; only then are the pgvector rows orphaned, and
  the operator drops the ``vector_index`` schema manually if they
  want the disk back.

Usage:
    # Migrate every indexed chunk
    python scripts/migrate_pgvector_to_seahorsedb.py

    # Limit to a single vault (recommended for first runs)
    python scripts/migrate_pgvector_to_seahorsedb.py --vault sdb-test

    # Tune batch size (default 64; larger = fewer round trips but a
    # single Coral 500 wastes more work)
    python scripts/migrate_pgvector_to_seahorsedb.py --batch 128

    # Just count what would be migrated, don't write
    python scripts/migrate_pgvector_to_seahorsedb.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Importable from the repo root: `python -m backend.scripts.migrate_...`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db.postgres import get_pool
from app.services import sparse_encoder
from app.services.vector_store.pgvector import PgvectorStore
from app.services.vector_store.seahorse_db import SeahorseDbStore


logger = logging.getLogger("akb.migrate")


async def _count_pending(
    pool, vault: str | None,
) -> int:
    """How many indexed-in-pgvector chunks the script would touch."""
    args: list = []
    where = ["c.vector_indexed_at IS NOT NULL", "vi.chunk_id IS NOT NULL"]
    if vault:
        where.append("v.name = $1")
        args.append(vault)
    sql = f"""
        SELECT COUNT(*)
          FROM chunks c
          JOIN vaults v ON v.id = c.vault_id
          JOIN vector_index.chunks vi ON vi.chunk_id = c.id
         WHERE {" AND ".join(where)}
    """
    async with pool.acquire() as conn:
        return int(await conn.fetchval(sql, *args))


async def _iter_chunks(
    pool, vault: str | None, batch: int,
):
    """Yield batches of (chunk_id, content, section_path, chunk_index,
    source_type, source_id, dense). Dense comes back as ``list[float]``
    because the pool has the pgvector binary codec registered."""
    # Register the binary vector codec on this script's pool too —
    # PgvectorStore does this for its own pool but we're reading from
    # the main pool here.
    from pgvector.asyncpg import register_vector

    args: list = []
    where = ["c.vector_indexed_at IS NOT NULL", "vi.chunk_id IS NOT NULL"]
    if vault:
        where.append("v.name = $1")
        args.append(vault)
    sql = f"""
        SELECT c.id::text       AS chunk_id,
               c.content        AS content,
               c.section_path   AS section_path,
               c.chunk_index    AS chunk_index,
               c.source_type    AS source_type,
               c.source_id::text AS source_id,
               vi.dense         AS dense
          FROM chunks c
          JOIN vaults v ON v.id = c.vault_id
          JOIN vector_index.chunks vi ON vi.chunk_id = c.id
         WHERE {" AND ".join(where)}
         ORDER BY c.created_at ASC, c.id ASC
    """

    async with pool.acquire() as conn:
        await register_vector(conn)
        async with conn.transaction():
            cur = await conn.cursor(sql, *args)
            buf: list[dict] = []
            while True:
                rows = await cur.fetch(batch)
                if not rows:
                    if buf:
                        yield buf
                    break
                for r in rows:
                    buf.append(dict(r))
                if len(buf) >= batch:
                    yield buf
                    buf = []


async def migrate(
    vault: str | None,
    batch: int,
    dry_run: bool,
) -> None:
    if settings.vector_store_driver != "seahorse-db" and not dry_run:
        raise SystemExit(
            "Refusing to migrate: settings.vector_store_driver is "
            f"{settings.vector_store_driver!r}, not 'seahorse-db'. "
            "Flip vector_store_driver in app.yaml first so "
            "sparse_encoder produces the raw-TF weights the seahorse-db "
            "driver expects (see sparse_encoder docstring)."
        )

    pool = await get_pool()
    total = await _count_pending(pool, vault)
    if total == 0:
        logger.info("Nothing to migrate (no chunks indexed in vector_index).")
        return
    logger.info("Will migrate %d chunks (vault=%s, batch=%d)", total, vault or "<all>", batch)
    if dry_run:
        return

    # We don't go through the factory singleton — that one might be
    # configured for pgvector still if the backend is mid-restart.
    # Build a fresh seahorse-db store from settings.
    target = SeahorseDbStore(
        coordinator_url=settings.seahorsedb_coordinator_url,
        table_name=settings.seahorsedb_table_name,
        dense_dim=settings.embed_dimensions,
        distance=settings.seahorsedb_distance,
        auto_create=settings.seahorsedb_auto_create,
        timeout=settings.seahorsedb_request_timeout_secs,
    )
    await target.ensure_collection()

    # Keep a separate handle on the source for symmetry / future inverse
    # migrations. Currently unused after construction — the script reads
    # rows out of vector_index.chunks via raw SQL above instead of
    # touching PgvectorStore methods, since the driver's public surface
    # is upsert/search-oriented.
    PgvectorStore(
        dsn=None,
        schema=settings.vector_store_schema,
        dense_dim=settings.embed_dimensions,
        sparse_shape=settings.vector_store_sparse_shape,
        get_main_pool=get_pool,
    )

    started = time.monotonic()
    seen = 0
    failed = 0
    log_every = max(50, total // 20)

    async for batch_rows in _iter_chunks(pool, vault, batch):
        for row in batch_rows:
            # Sparse: re-encode from content with the active driver's
            # convention. settings is seahorse-db at this point so the
            # encoder will emit raw TF.
            sparse_indices, sparse_values = await sparse_encoder.encode_document(
                row["content"] or "",
            )
            dense = row["dense"]
            if dense is None:
                # Embed-disabled rows in pgvector are stored as NULL. The
                # seahorse-db schema can't accept those (see CHANGELOG
                # 0.7.6). Skip them and let the operator deal manually.
                logger.warning(
                    "skipping chunk %s: dense is NULL (embed disabled when first indexed)",
                    row["chunk_id"],
                )
                failed += 1
                continue
            try:
                # pgvector's asyncpg codec yields numpy.float32 elements;
                # json.dumps in the driver can't serialise those. Cast
                # to Python float once at the boundary instead of
                # plumbing numpy awareness into the driver.
                dense_py = [float(x) for x in dense]
                await target.upsert_one(
                    chunk_id=row["chunk_id"],
                    content=row["content"] or "",
                    section_path=row["section_path"],
                    chunk_index=int(row["chunk_index"] or 0),
                    dense=dense_py,
                    sparse_indices=sparse_indices,
                    sparse_values=sparse_values,
                    source_type=row["source_type"] or "document",
                    source_id=row["source_id"],
                )
            except Exception as e:  # noqa: BLE001
                logger.error("upsert failed for chunk %s: %s", row["chunk_id"], e)
                failed += 1
                continue

            seen += 1
            if seen % log_every == 0:
                rate = seen / max(time.monotonic() - started, 0.001)
                eta = (total - seen) / max(rate, 0.001)
                logger.info(
                    "%d/%d (%.1f%%) done, %.1f chunks/sec, eta ~%ds",
                    seen, total, 100.0 * seen / total, rate, int(eta),
                )

    elapsed = time.monotonic() - started
    logger.info(
        "Migration complete: %d succeeded, %d failed, %.1fs total (%.1f chunks/sec).",
        seen, failed, elapsed, seen / max(elapsed, 0.001),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate pgvector chunks → seahorse-db without re-embedding.",
    )
    parser.add_argument("--vault", help="restrict to one vault by name")
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(migrate(args.vault, args.batch, args.dry_run))


if __name__ == "__main__":
    main()
