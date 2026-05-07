"""Migration 016: drop chunks.embedding + embed_* columns.

Phase 4 of the vector-store driver abstraction (see
docs/superpowers/specs/2026-05-07-vector-store-driver-abstraction.md).

Source-of-truth split flips: main PG holds chunk text + metadata only;
the *vector store* (Qdrant or pgvector — operator's choice) owns the
dense embedding and the corpus-side BM25 sparse vector. Re-indexing
from text is always cheap because vocab + tokenizer + embedding model
are deterministic functions of (content, model_id).

Concrete changes:
- Drop chunks.embedding (was vector(N), forced main PG to load
  pgvector extension even when the operator wanted a separate vector
  store).
- Drop embed_next_attempt_at, embed_retry_count, embed_last_error.
  The legacy two-stage pipeline (embed_worker → vector_indexer) is
  collapsed into a single "indexing" stage now driven entirely by
  vector_*_at columns.
- Force-NULL all rows' vector_indexed_at so the unified indexer
  re-populates the vector store from text. Operators should expect a
  one-time re-index window proportional to chunk count × embed
  throughput (~60-120 min for ~600k chunks at typical OpenAI-compatible
  endpoint speeds).

Idempotent on a partially-applied DB (DROP IF EXISTS).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.016")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # Skip-if-already-applied guard. Without this, every backend
    # startup re-runs step 5 below and force-NULLs vector_indexed_at on
    # every successfully-indexed chunk — wiping out all accumulated
    # indexing work since the last restart. Steps 1-4 are individually
    # guarded by IF EXISTS, but the absence of `chunks.embedding` is
    # the canonical signal that this migration's destructive bulk
    # update has already run and must not run again.
    embedding_exists = await conn.fetchval(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name   = 'chunks'
           AND column_name  = 'embedding'
        """
    )
    if embedding_exists is None:
        logger.info(
            "Migration 016 already applied (chunks.embedding absent); "
            "skipping force re-index"
        )
        return

    # 1. Drop embedding column. The dense vector now lives exclusively
    # in the configured vector store. We don't try to preserve the
    # data — the indexer rebuilds from chunks.content.
    await conn.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS embedding")

    # 2. Drop the partial index that referenced `embedding` (if any).
    # Migration 005's idx_chunks_vector_pending was on (vector_next_attempt_at)
    # WHERE vector_indexed_at IS NULL — predicate doesn't reference
    # embedding, so it survives the column drop. But there was a legacy
    # `idx_chunks_embedding` (HNSW on embedding) that may exist on some
    # deployments; drop it explicitly.
    await conn.execute("DROP INDEX IF EXISTS idx_chunks_embedding")

    # 3. Drop the legacy embed_* status columns.
    await conn.execute(
        """
        ALTER TABLE chunks
            DROP COLUMN IF EXISTS embed_next_attempt_at,
            DROP COLUMN IF EXISTS embed_retry_count,
            DROP COLUMN IF EXISTS embed_last_error
        """
    )

    # 4. Drop indexes that referenced the dropped embed_* columns.
    # Migration 003 created idx_chunks_embed_pending; harmless to drop
    # whether or not it still exists.
    await conn.execute("DROP INDEX IF EXISTS idx_chunks_embed_pending")

    # 5. Force re-index. The unified indexer (embed_worker, post-Phase-4)
    # picks up rows where vector_indexed_at IS NULL and pushes them
    # through the embedding API + sparse encoder + vector store upsert
    # in one atomic operation.
    #
    # Chunked UPDATE rather than one big sweep — at ~600k rows the
    # whole-table UPDATE comfortably blows past asyncpg's default
    # command_timeout. Per-batch UPDATEs commit incrementally; if
    # this migration is interrupted partway through, the next
    # startup picks up only the rows still bearing a non-NULL
    # vector_indexed_at and finishes the job. Idempotent.
    BATCH = 50_000
    total = 0
    while True:
        n = await conn.fetchval(
            """
            WITH batch AS (
                SELECT id FROM chunks
                 WHERE vector_indexed_at IS NOT NULL
                 LIMIT $1
            ),
            affected AS (
                UPDATE chunks c
                   SET vector_indexed_at = NULL,
                       vector_next_attempt_at = NULL,
                       vector_retry_count = 0,
                       vector_last_error = NULL
                  FROM batch
                 WHERE c.id = batch.id
                RETURNING 1
            )
            SELECT COUNT(*) FROM affected
            """,
            BATCH,
            timeout=120.0,  # generous: ~50k row UPDATE is usually < 30s
        )
        n = int(n or 0)
        total += n
        if n == 0:
            break
        logger.info("Migration 016: cleared %d rows (running total %d)", n, total)
    logger.info(
        "Migration 016 applied: dropped chunks.embedding + embed_* cols; "
        "force-NULLed vector_indexed_at on %d rows so the indexing "
        "worker re-populates the vector store from text",
        total,
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
