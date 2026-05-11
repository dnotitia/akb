"""Background worker: drain vector_delete_outbox → vector_store.delete_point.

Post-Phase-4 the upsert side is owned by `embed_worker` (which now does
embed + sparse + upsert atomically — main PG no longer stores the dense
vector). What's left here is the delete pipeline:

- When chunks are removed from main PG, `enqueue_source_deletes` (called
  inside the same transaction as the chunks DELETE) records each
  chunk_id in `vector_delete_outbox`. That row outlives the chunks
  table — main PG forgets the chunk, but the outbox preserves the
  identifier the vector store needs to forget it too.
- This worker drains the outbox: vector_store.delete_point per row,
  retry-on-failure with backoff, periodic sweep of long-processed rows.

Indexing-queue stats (`pending_stats`) live in `embed_worker` — that's
where the indexing actually happens. This module owns delete only.

Loop mechanics live in `_backfill`.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner, MAX_RETRIES, next_attempt_delay
from app.services.vector_store import VectorStoreUnavailable, get_vector_store

logger = logging.getLogger("akb.delete_worker")

BATCH_SIZE = 16

# Outbox sweep cadence + retention window.
SWEEP_GRACE_INTERVAL = "1 day"
SWEEP_INTERVAL_SECONDS = 3600.0
_last_sweep_at: float = 0.0


# ── Delete pipeline ───────────────────────────────────────────────


async def _claim_delete_batch(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        WITH pending AS (
            SELECT id
              FROM vector_delete_outbox
             WHERE processed_at IS NULL
               AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
               AND retry_count < $2
             ORDER BY next_attempt_at NULLS FIRST, id
             LIMIT $1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE vector_delete_outbox o
           SET next_attempt_at = NOW() + INTERVAL '10 minutes'
          FROM pending p
         WHERE o.id = p.id
        RETURNING o.id, o.chunk_id, o.source_type, o.source_id, o.retry_count
        """,
        BATCH_SIZE, MAX_RETRIES,
    )
    return [dict(r) for r in rows]


async def _mark_delete_success(conn, outbox_id) -> None:
    await conn.execute(
        "UPDATE vector_delete_outbox SET processed_at = NOW(), last_error = NULL WHERE id = $1",
        outbox_id,
    )


async def _mark_delete_failure(conn, outbox_id, retry_count: int, error: str) -> None:
    delay = next_attempt_delay(retry_count)
    next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    await conn.execute(
        """
        UPDATE vector_delete_outbox
           SET retry_count = retry_count + 1,
               last_error = $2,
               next_attempt_at = $3
         WHERE id = $1
        """,
        outbox_id, (error or "")[:500], next_at,
    )


async def _process_deletes_once() -> int:
    store = get_vector_store()
    pool = await get_pool()

    # Stage 1: claim. Tiny transaction.
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch = await _claim_delete_batch(conn)
    if not batch:
        return 0

    # Stage 2: per-row atomic delete. Pass conn to vector_store so the
    # pgvector driver removes its row in the same transaction as the
    # outbox mark — outer rollback can no longer leave a dangling
    # vector_index row.
    succeeded = 0
    for row in batch:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await store.delete_point(str(row["chunk_id"]), conn=conn)
                    await _mark_delete_success(conn, row["id"])
        except VectorStoreUnavailable as e:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await _mark_delete_failure(conn, row["id"], row["retry_count"], str(e))
            return succeeded
        except Exception as e:  # noqa: BLE001
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await _mark_delete_failure(conn, row["id"], row["retry_count"], str(e))
            continue
        succeeded += 1

    return succeeded


# ── Outbox helpers ────────────────────────────────────────────────


async def enqueue_source_deletes(source_type: str, source_id: str, conn=None) -> int:
    """Enqueue delete entries for every chunk of an indexable source.
    Must be called BEFORE the PG chunks DELETE fires, in the same
    transaction, so chunk ids land in the outbox even if the chunks
    table forgets them."""
    src_uuid = source_id if isinstance(source_id, uuid.UUID) else uuid.UUID(str(source_id))
    sql_select = "SELECT id FROM chunks WHERE source_type = $1 AND source_id = $2"
    sql_insert = """
        INSERT INTO vector_delete_outbox
            (chunk_id, source_type, source_id, next_attempt_at)
        VALUES ($1, $2, $3, NOW())
    """

    async def _run(c):
        rows = await c.fetch(sql_select, source_type, src_uuid)
        count = 0
        for r in rows:
            await c.execute(sql_insert, r["id"], source_type, src_uuid)
            count += 1
        return count

    if conn is not None:
        return await _run(conn)
    pool = await get_pool()
    async with pool.acquire() as c:
        return await _run(c)


# ── Sweep ─────────────────────────────────────────────────────────


async def _sweep_outbox_once() -> int:
    """Purge outbox rows whose vector-store delete completed more than
    SWEEP_GRACE_INTERVAL ago. Rate-limited."""
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at < SWEEP_INTERVAL_SECONDS:
        return 0
    _last_sweep_at = now
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            WITH d AS (
                DELETE FROM vector_delete_outbox
                 WHERE processed_at IS NOT NULL
                   AND processed_at < NOW() - $1::interval
                RETURNING 1
            )
            SELECT COUNT(*) FROM d
            """,
            SWEEP_GRACE_INTERVAL,
        )
    n = int(n or 0)
    if n:
        logger.info("outbox sweep: purged %d rows", n)
    return n


# ── Abandoned-chunk reaper ────────────────────────────────────────

# A chunk is "abandoned" once vector_retry_count has hit MAX_RETRIES:
# the indexing worker has stopped picking it and it will sit in
# `vector_indexed_at IS NULL` forever. Operators see them as a stuck
# `indexing N` counter on the UI. After this grace window we delete
# them from PG (enqueuing the vector-store cleanup via the outbox)
# so the counter clears itself.
#
# The grace window lets an operator notice + investigate the failure
# (e.g. an oversize chunk from a bug in the chunker) before the row
# is reclaimed. 7d is generous; tune via REAP_GRACE_INTERVAL if needed.
REAP_GRACE_INTERVAL = "7 days"
REAP_INTERVAL_SECONDS = 3600.0
_last_reap_at: float = 0.0


async def _reap_abandoned_chunks_once() -> int:
    """Delete chunks whose `vector_retry_count >= MAX_RETRIES` and whose
    last retry attempt was more than REAP_GRACE_INTERVAL ago. Enqueues
    them to `vector_delete_outbox` so this worker's normal delete pass
    removes them from the vector store too. Rate-limited."""
    global _last_reap_at
    now = time.monotonic()
    if now - _last_reap_at < REAP_INTERVAL_SECONDS:
        return 0
    _last_reap_at = now

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            n = await conn.fetchval(
                """
                WITH abandoned AS (
                    SELECT id, source_type, source_id
                      FROM chunks
                     WHERE vector_indexed_at IS NULL
                       AND vector_retry_count >= $1
                       AND (
                           vector_next_attempt_at IS NULL
                        OR vector_next_attempt_at < NOW() - $2::interval
                       )
                     FOR UPDATE SKIP LOCKED
                ),
                enqueued AS (
                    INSERT INTO vector_delete_outbox
                        (chunk_id, source_type, source_id, next_attempt_at)
                    SELECT id, source_type, source_id, NOW() FROM abandoned
                    RETURNING 1
                ),
                deleted AS (
                    DELETE FROM chunks WHERE id IN (SELECT id FROM abandoned)
                    RETURNING 1
                )
                SELECT COUNT(*) FROM deleted
                """,
                MAX_RETRIES, REAP_GRACE_INTERVAL,
            )
    n = int(n or 0)
    if n:
        logger.info("abandoned-chunk reap: removed %d rows (outbox enqueued)", n)
    return n


# ── Main loop ─────────────────────────────────────────────────────


async def _process_once() -> int:
    try:
        d = await _process_deletes_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("delete_worker delete pass failed: %s", e)
        d = 0
    try:
        await _sweep_outbox_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("delete_worker outbox sweep failed: %s", e)
    try:
        await _reap_abandoned_chunks_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("delete_worker abandoned-chunk reap failed: %s", e)
    return d


_runner = BackfillRunner("delete_worker", _process_once)
start = _runner.start
stop = _runner.stop


# ── Outbox-only stats ─────────────────────────────────────────────


async def delete_outbox_stats() -> dict:
    """Snapshot of the delete outbox state.

    Returns `{pending, abandoned}` for the global view. Indexing-queue
    stats (the chunks-level `pending/retrying/abandoned/indexed` slice)
    live in `embed_worker.pending_stats` — that's where the actual
    indexing happens.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE processed_at IS NULL AND retry_count < $1)     AS pending,
                COUNT(*) FILTER (WHERE processed_at IS NULL AND retry_count >= $1)    AS abandoned
              FROM vector_delete_outbox
            """,
            MAX_RETRIES,
        )
    return {
        "pending":   int(row["pending"]),
        "abandoned": int(row["abandoned"]),
    }
