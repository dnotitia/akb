"""Background worker: drain `s3_delete_outbox` → `s3_adapter.delete`.

When a `vault_files` row is removed, the service inserts the
matching `s3_key` into `s3_delete_outbox` inside the same PG TX.
This worker picks those rows up and removes the underlying S3
objects, retrying with backoff on transient failures.

Why an outbox: a crash between PG commit and the S3 call would
otherwise orphan an S3 object (DB row gone, blob still billed) or,
the other way, double-issue an S3 delete. The outbox row makes the
async S3 step durable and exactly-once-ish (S3 delete is idempotent
anyway, so re-issue is safe).

Mirrors `delete_worker.py` (vector store) shape — same loop,
backoff, claim, sweep.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.services._backfill import BackfillRunner, MAX_RETRIES, next_attempt_delay
from app.services.adapters import s3_adapter

logger = logging.getLogger("akb.s3_delete_worker")

BATCH_SIZE = 16

SWEEP_GRACE_INTERVAL = "1 day"
SWEEP_INTERVAL_SECONDS = 3600.0
_last_sweep_at: float = 0.0


# ── Outbox helpers (called by services in their TX) ──────────────


async def enqueue_delete(conn, s3_key: str) -> None:
    """Enqueue an S3 object for asynchronous deletion. MUST be called
    inside the same TX as the DB write that removes the row pointing
    at this object — that's the only way to guarantee no orphan."""
    await conn.execute(
        "INSERT INTO s3_delete_outbox (s3_key, next_attempt_at) VALUES ($1, NOW())",
        s3_key,
    )


# ── Claim / mark ─────────────────────────────────────────────────


async def _claim_batch(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        WITH pending AS (
            SELECT id
              FROM s3_delete_outbox
             WHERE processed_at IS NULL
               AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
               AND retry_count < $2
             ORDER BY next_attempt_at NULLS FIRST, id
             LIMIT $1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE s3_delete_outbox o
           SET next_attempt_at = NOW() + INTERVAL '10 minutes'
          FROM pending p
         WHERE o.id = p.id
        RETURNING o.id, o.s3_key, o.retry_count
        """,
        BATCH_SIZE, MAX_RETRIES,
    )
    return [dict(r) for r in rows]


async def _mark_success(conn, outbox_id) -> None:
    await conn.execute(
        "UPDATE s3_delete_outbox SET processed_at = NOW(), last_error = NULL WHERE id = $1",
        outbox_id,
    )


async def _mark_failure(conn, outbox_id, retry_count: int, error: str) -> None:
    delay = next_attempt_delay(retry_count)
    next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    await conn.execute(
        """
        UPDATE s3_delete_outbox
           SET retry_count = retry_count + 1,
               last_error = $2,
               next_attempt_at = $3
         WHERE id = $1
        """,
        outbox_id, (error or "")[:500], next_at,
    )


# ── Pipeline ─────────────────────────────────────────────────────


async def _process_deletes_once() -> int:
    pool = await get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            batch = await _claim_batch(conn)
    if not batch:
        return 0

    succeeded = 0
    for row in batch:
        try:
            s3_adapter.delete(row["s3_key"])
        except NotFoundError:
            # S3 object already absent — treat as success (delete is
            # idempotent; this also covers re-runs after a partial
            # failure).
            pass
        except Exception as e:  # noqa: BLE001
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await _mark_failure(conn, row["id"], row["retry_count"], str(e))
            continue

        async with pool.acquire() as conn:
            async with conn.transaction():
                await _mark_success(conn, row["id"])
        succeeded += 1

    return succeeded


# ── Sweep ────────────────────────────────────────────────────────


async def _sweep_outbox_once() -> int:
    """Purge processed rows older than SWEEP_GRACE_INTERVAL.
    Rate-limited to once per SWEEP_INTERVAL_SECONDS."""
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at < SWEEP_INTERVAL_SECONDS:
        return 0
    _last_sweep_at = now
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            f"""
            WITH d AS (
                DELETE FROM s3_delete_outbox
                 WHERE processed_at IS NOT NULL
                   AND processed_at < NOW() - INTERVAL '{SWEEP_GRACE_INTERVAL}'
                RETURNING 1
            )
            SELECT COUNT(*) FROM d
            """
        )
    n = int(n or 0)
    if n:
        logger.info("s3 outbox sweep: purged %d rows", n)
    return n


# ── Loop ─────────────────────────────────────────────────────────


async def _process_once() -> int:
    try:
        d = await _process_deletes_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("s3_delete_worker delete pass failed: %s", e)
        d = 0
    try:
        await _sweep_outbox_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("s3_delete_worker outbox sweep failed: %s", e)
    return d


_runner = BackfillRunner("s3_delete_worker", _process_once)
start = _runner.start
stop = _runner.stop


# ── Stats ────────────────────────────────────────────────────────


async def pending_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE processed_at IS NULL AND retry_count < $1)  AS pending,
                COUNT(*) FILTER (WHERE processed_at IS NULL AND retry_count >= $1) AS abandoned
              FROM s3_delete_outbox
            """,
            MAX_RETRIES,
        )
    return {
        "pending":   int(row["pending"]),
        "abandoned": int(row["abandoned"]),
    }
