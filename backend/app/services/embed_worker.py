"""Background indexing worker — atomic embed + sparse + vector-store upsert.

Replaces the legacy two-stage pipeline (NULL → embedding, then
embedded → vector store) with a single per-chunk atomic stage. Main PG no longer stores the dense vector, so there's no value
in splitting the work across two queues.

Per-batch flow:
  1. Claim transaction (very short): mark up to BATCH_SIZE chunks as
     in-flight by pushing their next_attempt_at +10min, then commit.
     SKIP LOCKED keeps peer workers off these rows.
  2. Embedding API call OUTSIDE any transaction: external service
     latency must never lock PG conns or rows.
  3. Per-chunk transaction: encode sparse + vector_store.upsert_one
     (passing this conn — pgvector driver joins our transaction so
     the vector_index INSERT and chunks UPDATE commit together) +
     mark_success. Atomic. Failures back off via mark_failure on a
     separate small transaction.

The per-chunk model means a vector-store crash mid-batch still leaves
the SoT consistent — only the half-processed chunk reverts and gets
re-claimed on the next cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.postgres import get_pool
from app.services import sparse_encoder
from app.services._backfill import BackfillRunner, MAX_RETRIES, next_attempt_delay
from app.services.index_service import generate_embeddings
from app.services.vector_store import VectorStoreUnavailable, get_vector_store

logger = logging.getLogger("akb.embed_worker")


def _batch_size() -> int:
    """Read BATCH_SIZE from config at call time so a `kubectl edit
    configmap` followed by a pod restart picks up the new value
    without a code change."""
    return int(getattr(settings, "indexing_batch_size", 16))


async def _claim_batch(conn) -> list[dict]:
    """Atomically claim up to BATCH_SIZE pending chunks. Pushes their
    next_attempt_at out so peer workers (other replicas) skip them via
    SKIP LOCKED.

    Ordering: newest chunks first. A user who just put a document
    expects search to surface it within seconds, not after the backlog
    (re-index after a model swap or migration 016 force-NULL) clears.
    `created_at DESC` keeps interactive writes hot while the backlog
    drains underneath. Retry-backoff (vector_next_attempt_at <= NOW())
    ensures chronically-failing rows still rotate fairly within their
    eligible window — they don't permanently starve.
    """
    rows = await conn.fetch(
        """
        WITH pending AS (
            SELECT id
              FROM chunks
             WHERE vector_indexed_at IS NULL
               AND (vector_next_attempt_at IS NULL OR vector_next_attempt_at <= NOW())
               AND vector_retry_count < $2
             ORDER BY created_at DESC, id
             LIMIT $1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE chunks c
           SET vector_next_attempt_at = NOW() + INTERVAL '10 minutes'
          FROM pending p
         WHERE c.id = p.id
        RETURNING c.id, c.source_type, c.source_id, c.vault_id,
                  c.content, c.section_path, c.chunk_index,
                  c.vector_retry_count
        """,
        _batch_size(), MAX_RETRIES,
    )
    return [dict(r) for r in rows]


async def _mark_success(conn, chunk_id) -> None:
    await conn.execute(
        """
        UPDATE chunks
           SET vector_indexed_at = NOW(),
               vector_last_error = NULL,
               vector_next_attempt_at = NULL
         WHERE id = $1
        """,
        chunk_id,
    )


async def _mark_failure(pool, chunk_id, retry_count: int, error: str) -> None:
    """Failures own a tiny transaction of their own. Done outside the
    upsert path so a single chunk's failure can't poison the rest of
    the batch."""
    delay = next_attempt_delay(retry_count)
    next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """
                UPDATE chunks
                   SET vector_retry_count = vector_retry_count + 1,
                       vector_last_error = $2,
                       vector_next_attempt_at = $3
                 WHERE id = $1
                """,
                chunk_id, (error or "")[:500], next_at,
            )


async def _process_once() -> int:
    """Process one batch. Returns successfully-indexed count."""
    pool = await get_pool()

    # Stage 1: claim. Tiny transaction; commits before any external
    # work begins.
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch = await _claim_batch(conn)
    if not batch:
        return 0

    # Stage 2: embedding API. Outside any PG transaction — the conn
    # pool stays free during the network round-trip.
    texts = [r["content"] or "" for r in batch]
    try:
        embeddings = await generate_embeddings(texts)
    except Exception as e:  # noqa: BLE001
        logger.warning("Embedding call crashed: %s", e)
        embeddings = []

    if not embeddings or len(embeddings) != len(batch):
        # Batch-wide failure (API down, malformed response).
        for row in batch:
            await _mark_failure(
                pool, row["id"], row["vector_retry_count"],
                "embedding API returned no/partial result",
            )
        return 0

    # Stage 3: per-chunk transaction. Each iteration is atomic over
    # vector_store.upsert + chunks.UPDATE — outer rollback can no
    # longer leave the vector store with an unmarked row.
    store = get_vector_store()
    succeeded = 0
    for row, dense in zip(batch, embeddings):
        if not dense:
            await _mark_failure(
                pool, row["id"], row["vector_retry_count"],
                "empty embedding vector",
            )
            continue

        content = row["content"] or ""
        try:
            sparse_idx, sparse_vals = await sparse_encoder.encode_document(content)
        except Exception as e:  # noqa: BLE001
            await _mark_failure(
                pool, row["id"], row["vector_retry_count"],
                f"sparse encode failed: {e}",
            )
            continue

        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await store.upsert_one(
                        conn=conn,  # pgvector joins this transaction
                        chunk_id=str(row["id"]),
                        content=content,
                        section_path=row["section_path"],
                        chunk_index=int(row["chunk_index"] or 0),
                        dense=dense,
                        sparse_indices=sparse_idx,
                        sparse_values=sparse_vals,
                        source_type=row["source_type"] or "document",
                        source_id=str(row["source_id"]),
                    )
                    await _mark_success(conn, row["id"])
        except VectorStoreUnavailable as e:
            await _mark_failure(
                pool, row["id"], row["vector_retry_count"], str(e),
            )
            # Vector store is down — short-circuit the rest of the batch
            # so we don't hammer it. Remaining rows already have
            # next_attempt_at set by _claim_batch.
            logger.info("vector store unavailable; backing off batch")
            return succeeded
        except Exception as e:  # noqa: BLE001
            await _mark_failure(
                pool, row["id"], row["vector_retry_count"], str(e),
            )
            continue

        succeeded += 1
    return succeeded


_runner = BackfillRunner("embed_worker", _process_once)
start = _runner.start
stop = _runner.stop


# ── Indexing-queue stats ──────────────────────────────────────────


async def pending_stats(vault_id=None) -> dict:
    """Snapshot of indexing-queue state. Lives here (not in
    `delete_worker`) because embed_worker is what actually drains the
    queue — naming follows responsibility.

    Returns:

        {
            "upsert": { pending, retrying, abandoned, indexed },
            "delete": { pending, abandoned },        # global view only
        }

    Vault-scoped callers (vault_id != None) get only the upsert slice
    — outbox rows don't carry vault_id (the chunks they refer to are
    already gone), so the delete slice can't be narrowed.
    """
    from app.services import delete_worker

    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault_id is None:
            chunk_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE vector_indexed_at IS NULL)                                          AS pending,
                    COUNT(*) FILTER (WHERE vector_indexed_at IS NULL
                                     AND vector_retry_count > 0 AND vector_retry_count < $1)                    AS retrying,
                    COUNT(*) FILTER (WHERE vector_indexed_at IS NULL AND vector_retry_count >= $1)             AS abandoned,
                    COUNT(*) FILTER (WHERE vector_indexed_at IS NOT NULL)                                       AS indexed
                  FROM chunks
                """,
                MAX_RETRIES,
            )
            return {
                "upsert": {
                    "pending":   int(chunk_row["pending"]),
                    "retrying":  int(chunk_row["retrying"]),
                    "abandoned": int(chunk_row["abandoned"]),
                    "indexed":   int(chunk_row["indexed"]),
                },
                "delete": await delete_worker.delete_outbox_stats(),
            }
        chunk_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE vector_indexed_at IS NULL)                                          AS pending,
                COUNT(*) FILTER (WHERE vector_indexed_at IS NULL
                                 AND vector_retry_count > 0 AND vector_retry_count < $1)                    AS retrying,
                COUNT(*) FILTER (WHERE vector_indexed_at IS NULL AND vector_retry_count >= $1)             AS abandoned,
                COUNT(*) FILTER (WHERE vector_indexed_at IS NOT NULL)                                       AS indexed
              FROM chunks
             WHERE vault_id = $2
            """,
            MAX_RETRIES, vault_id,
        )
        return {
            "upsert": {
                "pending":   int(chunk_row["pending"]),
                "retrying":  int(chunk_row["retrying"]),
                "abandoned": int(chunk_row["abandoned"]),
                "indexed":   int(chunk_row["indexed"]),
            },
        }
