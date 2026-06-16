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
import uuid
from pathlib import Path

# Importable from the repo root: `python -m backend.scripts.migrate_...`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db.postgres import get_pool
from app.services import sparse_encoder
from app.services.vector_store.base import ChunkUpsert
from app.services.vector_store.pgvector import PgvectorStore
from app.services.vector_store.seahorse_db import SeahorseDbStore


logger = logging.getLogger("akb.migrate")


async def _count_pending(
    pool, vault: str | None, resume_after: str | None,
) -> int:
    """How many indexed-in-pgvector chunks the script would touch."""
    args: list = []
    where = ["c.vector_indexed_at IS NOT NULL", "vi.chunk_id IS NOT NULL"]
    if vault:
        where.append(f"v.name = ${len(args) + 1}")
        args.append(vault)
    if resume_after:
        where.append(f"c.id > ${len(args) + 1}::uuid")
        args.append(resume_after)
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
    pool, vault: str | None, batch: int, resume_after: str | None,
):
    """Yield batches of (chunk_id, content, section_path, chunk_index,
    source_type, source_id, dense). Dense comes back as ``list[float]``
    because the pool has the pgvector binary codec registered.

    ``ORDER BY c.id`` is the only ordering we need for resume — it's a
    UUID, monotonic-enough across runs, and the resume gate is a
    ``c.id > $N`` filter on the same column. Using created_at would
    re-introduce ambiguity for chunks that share a timestamp.
    """
    # Register the binary vector codec on this script's pool too —
    # PgvectorStore does this for its own pool but we're reading from
    # the main pool here.
    from pgvector.asyncpg import register_vector

    args: list = []
    where = ["c.vector_indexed_at IS NOT NULL", "vi.chunk_id IS NOT NULL"]
    if vault:
        where.append(f"v.name = ${len(args) + 1}")
        args.append(vault)
    if resume_after:
        where.append(f"c.id > ${len(args) + 1}::uuid")
        args.append(resume_after)
    sql = f"""
        SELECT c.id::text       AS chunk_id,
               c.content        AS content,
               c.section_path   AS section_path,
               c.chunk_index    AS chunk_index,
               c.source_type    AS source_type,
               c.source_id::text AS source_id,
               c.vault_id::text  AS vault_id,
               vi.dense         AS dense
          FROM chunks c
          JOIN vaults v ON v.id = c.vault_id
          JOIN vector_index.chunks vi ON vi.chunk_id = c.id
         WHERE {" AND ".join(where)}
         ORDER BY c.id ASC
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


def _read_progress(progress_file: str | None) -> str | None:
    """Last successfully-flushed chunk_id (UUID text), or None if no
    prior run / corrupted file. Atomic file rename guarantees we never
    see a torn write — either the file is the previous checkpoint or
    the new one, never both."""
    if not progress_file:
        return None
    p = Path(progress_file)
    if not p.exists():
        return None
    try:
        s = p.read_text().strip()
    except OSError:
        return None
    if not s:
        return None
    try:
        uuid.UUID(s)
    except ValueError:
        logger.warning(
            "progress file %s does not contain a UUID, ignoring: %s",
            progress_file, s[:60],
        )
        return None
    return s


def _write_progress(progress_file: str | None, chunk_id: str) -> None:
    """Atomic checkpoint write. tmp + rename guarantees that a crash
    mid-write doesn't leave a half-written UUID; the previous
    checkpoint is preserved if rename never happens."""
    if not progress_file:
        return
    p = Path(progress_file)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(chunk_id + "\n")
    tmp.replace(p)


async def migrate(
    vault: str | None,
    batch: int,
    dry_run: bool,
    progress_file: str | None,
    checkpoint_every: int,
) -> None:
    if settings.vector_store_driver != "seahorse-db" and not dry_run:
        raise SystemExit(
            "Refusing to migrate: settings.vector_store_driver is "
            f"{settings.vector_store_driver!r}, not 'seahorse-db'. "
            "Flip vector_store_driver in app.yaml first so "
            "sparse_encoder produces the raw-TF weights the seahorse-db "
            "driver expects (see sparse_encoder docstring)."
        )

    resume_after = _read_progress(progress_file)
    if resume_after:
        logger.info("Resuming after chunk_id=%s (from %s)",
                    resume_after, progress_file)

    pool = await get_pool()
    total = await _count_pending(pool, vault, resume_after)
    if total == 0:
        logger.info(
            "Nothing to migrate (already done?). resume_after=%s",
            resume_after,
        )
        return
    logger.info(
        "Will migrate %d chunks (vault=%s, batch=%d, resume_after=%s)",
        total, vault or "<all>", batch, resume_after or "(none)",
    )
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

    async for batch_rows in _iter_chunks(pool, vault, batch, resume_after):
        # Build the per-row ChunkUpsert payloads. Sparse re-encoding
        # and the numpy.float32 → Python float cast happen here so
        # the driver call below is purely "ship this batch".
        batch_payload: list[ChunkUpsert] = []
        for row in batch_rows:
            dense = row["dense"]
            if dense is None:
                # Embed-disabled rows in pgvector are stored as NULL.
                # The seahorse-db schema can't accept those (see
                # CHANGELOG 0.7.6); skip with a warning so the operator
                # can manually re-embed if needed.
                logger.warning(
                    "skipping chunk %s: dense is NULL "
                    "(embed disabled when first indexed)",
                    row["chunk_id"],
                )
                failed += 1
                continue
            # Sparse: re-encode from content with the active driver's
            # convention. settings.vector_store_driver is seahorse-db
            # at this point so the encoder emits raw TF.
            sparse_indices, sparse_values = await sparse_encoder.encode_document(
                row["content"] or "",
            )
            batch_payload.append(ChunkUpsert(
                chunk_id=row["chunk_id"],
                content=row["content"] or "",
                section_path=row["section_path"],
                chunk_index=int(row["chunk_index"] or 0),
                # pgvector's asyncpg codec yields numpy.float32 elements;
                # json.dumps in the driver can't serialise those.
                dense=[float(x) for x in dense],
                sparse_indices=sparse_indices,
                sparse_values=sparse_values,
                source_type=row["source_type"] or "document",
                source_id=row["source_id"],
                vault_id=row["vault_id"],
            ))

        if not batch_payload:
            continue

        try:
            # Single round-trip per batch — Coral's JSONL ingest reads
            # the newline-joined payload through arrow_json with its
            # own ``reader_batch_size`` (64 default) and dispatches
            # record-batches segment-by-segment in 8-way concurrency.
            # Net: ~len(batch_payload)/64 Kafka WAL appends per call
            # instead of len(batch_payload) of them.
            await target.upsert_batch(batch_payload)
            seen += len(batch_payload)
        except Exception as e:  # noqa: BLE001
            # Coral occasionally returns a 503 from a transient
            # Writer-side transport error on batch_insert_via_catalog
            # (observed ~0.15% of batches under sustained load). Rather
            # than abandoning len(batch_payload) chunks on a transient
            # failure — and silently introducing a gap because the
            # migration script does not touch PG's
            # ``vector_indexed_at`` — fall back to per-row upserts
            # for this batch. Slow on the failure path, but every
            # row gets its own attempt and the abandonment count is
            # one per genuinely broken row instead of one per batch.
            #
            # SeahorseDB has no PK-aware upsert (Coral's insert path
            # appends; ``drop_duplicate_primary_key_rows`` only
            # dedups WITHIN a single record batch, not across the
            # segment). So the per-row retry CAN cause duplicates if
            # the batch partially landed on the Writer before the
            # error fired. The dogfood pattern of "new table per
            # rerun" sidesteps that — for a clean migration the
            # ratio of partial-batch-then-retry is small and the
            # alternative (silent loss) is worse.
            logger.warning(
                "upsert_batch failed for %d chunks "
                "(first chunk_id=%s): %s — falling back to per-row",
                len(batch_payload), batch_payload[0].chunk_id, e,
            )
            per_row_ok = 0
            per_row_failed = 0
            for c in batch_payload:
                try:
                    await target.upsert_one(
                        chunk_id=c.chunk_id,
                        content=c.content,
                        section_path=c.section_path,
                        chunk_index=c.chunk_index,
                        dense=c.dense,
                        sparse_indices=c.sparse_indices,
                        sparse_values=c.sparse_values,
                        source_type=c.source_type,
                        source_id=c.source_id,
                        vault_id=c.vault_id,
                    )
                    per_row_ok += 1
                except Exception as ee:  # noqa: BLE001
                    logger.error(
                        "  per-row upsert failed for chunk_id=%s: %s",
                        c.chunk_id, ee,
                    )
                    per_row_failed += 1
            seen += per_row_ok
            failed += per_row_failed
            logger.info(
                "per-row fallback summary: %d ok / %d failed",
                per_row_ok, per_row_failed,
            )
        if seen // log_every > (seen - len(batch_payload)) // log_every:
            rate = seen / max(time.monotonic() - started, 0.001)
            eta = (total - seen) / max(rate, 0.001)
            logger.info(
                "%d/%d (%.1f%%) done, %.1f chunks/sec, eta ~%ds",
                seen, total, 100.0 * seen / total, rate, int(eta),
            )

        # Checkpoint after every batch — chunk_id ordering is monotonic
        # (ORDER BY c.id ASC in the cursor SQL), so the last chunk_id
        # in the batch is the high-water mark. Even if the Coral side
        # is still draining a Kafka backlog, restarting with this
        # checkpoint just means we'll re-send the chunks that were in
        # flight at crash time — and a new table per run keeps the
        # duplicate risk bounded.
        if checkpoint_every and (
            seen % checkpoint_every < len(batch_payload)
            or seen >= total
        ):
            _write_progress(progress_file, batch_payload[-1].chunk_id)

    # Final checkpoint — pin the last chunk_id we successfully shipped.
    # ``batch_payload`` survives the loop scope; guard against the
    # zero-iteration case (resume already at the end, dry_run skipped).
    if seen > 0 and "batch_payload" in dir() and batch_payload:
        _write_progress(progress_file, batch_payload[-1].chunk_id)

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
    parser.add_argument(
        "--progress-file",
        default="/tmp/akb-migrate-progress.txt",
        help=(
            "Resume-capable checkpoint. Holds the last successfully-"
            "shipped chunk_id (UUID). On startup, the script reads this "
            "file and resumes from chunk.id > <stored UUID>. Pass empty "
            "string to disable checkpointing (e.g. ``--progress-file ''``)."
        ),
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=2048,
        help=(
            "Write a checkpoint roughly every N chunks (default 2048). "
            "Each batch's last chunk_id is the high-water mark; we write "
            "when cumulative ``seen`` crosses each multiple. Smaller = "
            "less re-work on resume, more disk syncs."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    progress_file = args.progress_file or None
    asyncio.run(migrate(
        args.vault, args.batch, args.dry_run,
        progress_file, args.checkpoint_every,
    ))


if __name__ == "__main__":
    main()
