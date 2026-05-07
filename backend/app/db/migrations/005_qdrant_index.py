"""Migration 005: additive schema for Qdrant-backed hybrid search.

This migration is strictly additive — it must never touch `chunks.embedding`
or delete any data. PG remains source of truth; Qdrant is a derived index
that can be rebuilt at any time from PG by resetting `vector_indexed_at`.

Adds:
- chunks.qdrant_* columns for per-chunk index tracking (mirror of embed_worker pattern)
- vector_delete_outbox table for async deletion (PG chunks are CASCADE-deleted;
  Qdrant cleanup is lazy via this outbox)
- bm25_vocab: append-only term → term_id mapping. IDs are never reassigned
  because Qdrant sparse vectors reference terms by integer id.
- bm25_stats: singleton row holding N, avgdl, tokenizer metadata.

Idempotent — safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.005")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # Per-chunk tracking of Qdrant index state. Mirrors embed_worker pattern.
    await conn.execute(
        """
        ALTER TABLE chunks
            ADD COLUMN IF NOT EXISTS vector_indexed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS vector_next_attempt_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS vector_retry_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS vector_last_error TEXT
        """
    )

    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunks_vector_pending
            ON chunks (vector_next_attempt_at)
         WHERE vector_indexed_at IS NULL
        """
    )

    # Outbox for Qdrant-side deletions. Populated when a chunk is deleted in PG.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_delete_outbox (
            id BIGSERIAL PRIMARY KEY,
            chunk_id UUID NOT NULL,
            document_id UUID,
            enqueued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processed_at TIMESTAMPTZ,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ,
            last_error TEXT
        )
        """
    )

    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vector_delete_pending
            ON vector_delete_outbox (next_attempt_at)
         WHERE processed_at IS NULL
        """
    )

    # BM25 vocabulary: append-only term → integer id.
    # Qdrant sparse vectors reference terms by id, so ids MUST be stable
    # across indexing and querying. Never reassign, never delete.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bm25_vocab (
            term TEXT PRIMARY KEY,
            term_id BIGINT NOT NULL UNIQUE,
            df BIGINT NOT NULL DEFAULT 0,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    await conn.execute(
        """
        CREATE SEQUENCE IF NOT EXISTS bm25_term_id_seq AS BIGINT START 1
        """
    )

    # Singleton row for corpus-level BM25 stats + tokenizer version.
    # Recalculable from chunks+vocab; safe to lose.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bm25_stats (
            id INTEGER PRIMARY KEY DEFAULT 1,
            total_docs BIGINT NOT NULL DEFAULT 0,
            avgdl DOUBLE PRECISION NOT NULL DEFAULT 0,
            tokenizer_name TEXT NOT NULL DEFAULT 'kiwi',
            tokenizer_version TEXT NOT NULL DEFAULT '0',
            k1 DOUBLE PRECISION NOT NULL DEFAULT 1.5,
            b DOUBLE PRECISION NOT NULL DEFAULT 0.75,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (id = 1)
        )
        """
    )

    await conn.execute(
        """
        INSERT INTO bm25_stats (id) VALUES (1)
        ON CONFLICT (id) DO NOTHING
        """
    )

    logger.info("Migration 005 applied: Qdrant index tracking + BM25 vocab/stats")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
