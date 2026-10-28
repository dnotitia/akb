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

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories import vault_files_repo
from app.services._backfill import BackfillRunner, MAX_RETRIES, next_attempt_delay
from app.services.adapters import s3_adapter

logger = logging.getLogger("akb.s3_delete_worker")

BATCH_SIZE = 16
# A presigned PUT may already be in flight when its metadata is deleted. Issue
# one immediate idempotent delete and one delayed reconciliation delete after
# the normal pending-upload lifecycle has elapsed.
PENDING_UPLOAD_DELETE_RECHECK_SECONDS = 24 * 60 * 60

SWEEP_GRACE_INTERVAL = "1 day"
SWEEP_INTERVAL_SECONDS = 3600.0
_last_sweep_at: float = 0.0

# This namespace is never published directly: confirmation copies to a fresh
# key outside it. Inventory, unlike a one-shot delete, also finds an accepted
# PUT that completes after the issuance-time cleanup has already run.
REPLACEMENT_STAGING_PREFIX = "__akb_file_replacements__/"
REPLACEMENT_STAGING_RETENTION_SECONDS = 3600 + 300
STAGING_SCAN_INTERVAL_SECONDS = 60
STAGING_SCAN_PAGE_SIZE = 100
_last_staging_scan_at: float = 0.0
_staging_scan_cursor: str | None = None


# ── Outbox helpers (called by services in their TX) ──────────────


async def enqueue_delete(conn, s3_key: str, *, delay_seconds: int = 0) -> int:
    """Enqueue an S3 object for asynchronous deletion.

    Row-owned objects must be enqueued inside the same transaction as the DB
    mutation that disowns them. ``delay_seconds`` also supports staging
    objects that must remain available until a presigned capability expires.
    """
    return await conn.fetchval(
        """
        INSERT INTO s3_delete_outbox (s3_key, next_attempt_at)
        VALUES ($1, NOW() + ($2 * INTERVAL '1 second'))
        RETURNING id
        """,
        s3_key, delay_seconds,
    )


async def enqueue_pending_upload_delete(conn, s3_key: str) -> None:
    """Delete now and reconcile once after any accepted PUT has settled."""
    await enqueue_delete(conn, s3_key)
    await enqueue_delete(
        conn,
        s3_key,
        delay_seconds=PENDING_UPLOAD_DELETE_RECHECK_SECONDS,
    )


async def cancel_delete(conn, outbox_id: int) -> None:
    """Cancel a delayed cleanup after its object becomes database-owned.

    Callers must do this in the same transaction that publishes the object.
    The outbox row then resolves commit ambiguity atomically: either both the
    publication and cancellation commit, or the pending cleanup remains.
    """
    await conn.execute(
        """
        UPDATE s3_delete_outbox
           SET processed_at = NOW(), retry_count = 0, last_error = NULL,
               next_attempt_at = NULL, claimed_at = NULL, abandoned_at = NULL
         WHERE id = $1 AND processed_at IS NULL
        """,
        outbox_id,
    )


# ── Claim / mark ─────────────────────────────────────────────────


async def _claim_batch(conn, limit: int = BATCH_SIZE) -> list[dict]:
    rows = await conn.fetch(
        """
        WITH pending AS (
            SELECT id
              FROM s3_delete_outbox
             WHERE processed_at IS NULL
               AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
               AND retry_count < $2
               AND abandoned_at IS NULL
             ORDER BY next_attempt_at NULLS FIRST, id
             LIMIT $1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE s3_delete_outbox o
           SET next_attempt_at = NOW() + INTERVAL '10 minutes',
               claimed_at = NOW(),
               retry_count = retry_count + 1
          FROM pending p
         WHERE o.id = p.id
        RETURNING o.id, o.s3_key, o.retry_count, o.claimed_at
        """,
        limit, MAX_RETRIES,
    )
    return [dict(r) for r in rows]


async def _mark_success(conn, outbox_id) -> None:
    await conn.execute(
        """
        UPDATE s3_delete_outbox
           SET processed_at = NOW(), retry_count = 0, last_error = NULL,
               next_attempt_at = NULL, claimed_at = NULL, abandoned_at = NULL
         WHERE id = $1
        """,
        outbox_id,
    )


async def _mark_failure(conn, outbox_id, attempt_count: int, error: str) -> None:
    terminal = attempt_count >= MAX_RETRIES
    delay = next_attempt_delay(max(0, attempt_count - 1))
    next_at = None if terminal else datetime.now(timezone.utc) + timedelta(seconds=delay)
    await conn.execute(
        """
        UPDATE s3_delete_outbox
           SET last_error = $2,
               next_attempt_at = $3,
               claimed_at = NULL,
               abandoned_at = CASE WHEN $4 THEN NOW() ELSE NULL END
         WHERE id = $1
        """,
        outbox_id, (error or "")[:500], next_at, terminal,
    )


async def _release_claim(conn, outbox_id, *, delay_seconds: int = 1) -> None:
    await conn.execute(
        """
        UPDATE s3_delete_outbox
           SET retry_count = GREATEST(retry_count - 1, 0),
               next_attempt_at = NOW() + ($2 * INTERVAL '1 second'),
               claimed_at = NULL
         WHERE id = $1 AND processed_at IS NULL AND abandoned_at IS NULL
        """,
        outbox_id,
        delay_seconds,
    )


async def _record_outcome(pool, marker, *args) -> bool:
    """Record one outcome on a fresh connection without aborting the batch."""
    try:
        async with pool.acquire() as conn:
            await marker(conn, *args)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "s3 delete outcome recording failed for outbox %s: %s",
            args[0] if args else "unknown",
            exc,
        )
        return False


# ── Pipeline ─────────────────────────────────────────────────────


async def _process_deletes_once() -> int:
    pool = await get_pool()
    succeeded = 0
    # Claim one row at a time. A worst-case object-store retry therefore cannot
    # consume the ten-minute lease of rows waiting later in the same batch.
    for _ in range(BATCH_SIZE):
        async with pool.acquire() as claim_conn:
            async with claim_conn.transaction():
                batch = await _claim_batch(claim_conn, limit=1)
        if not batch:
            break
        row = batch[0]

        outcome = "failure"
        failure_error = "cleanup did not complete"
        try:
            async with pool.acquire() as lock_conn:
                locked = await vault_files_repo.try_lock_s3_key_for_cleanup(
                    lock_conn, row["s3_key"],
                )
                if not locked:
                    outcome = "release"
                else:
                    try:
                        # This lookup is intentionally global by physical
                        # object key. A recreated vault can legitimately own
                        # the same key; deleting it would corrupt that File.
                        referenced = await lock_conn.fetchval(
                            """SELECT EXISTS (SELECT 1 FROM vault_files WHERE s3_key = $1)
                                      OR EXISTS (SELECT 1 FROM user_avatars WHERE object_key = $1)""",
                            row["s3_key"],
                        )
                        if not referenced:
                            try:
                                # No PostgreSQL transaction is open across
                                # this remote call. The session lock plus the
                                # outbox barrier makes new writers choose a
                                # different key.
                                await asyncio.to_thread(
                                    s3_adapter.delete, row["s3_key"],
                                )
                            except NotFoundError:
                                pass
                        outcome = "success"
                    except Exception as exc:  # noqa: BLE001
                        outcome = "failure"
                        failure_error = str(exc)
                    finally:
                        try:
                            await vault_files_repo.unlock_s3_key_after_cleanup(
                                lock_conn, row["s3_key"],
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "s3 cleanup lock release failed for %s: %s",
                                "private account image" if row["s3_key"].startswith(".akb-account-assets/") else row["s3_key"],
                                exc,
                            )
        except Exception as exc:  # noqa: BLE001
            # A broken lock/probe connection affects only this claimed row.
            # Its lease expires if recording also fails; later rows still run.
            outcome = "failure"
            failure_error = str(exc)

        if outcome == "success":
            recorded = await _record_outcome(pool, _mark_success, row["id"])
        elif outcome == "release":
            recorded = await _record_outcome(pool, _release_claim, row["id"])
        else:
            recorded = await _record_outcome(
                pool,
                _mark_failure,
                row["id"],
                row["retry_count"],
                failure_error,
            )
        if recorded and outcome == "success":
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


def _is_replacement_staging_key(key: str) -> bool:
    if not key.startswith(REPLACEMENT_STAGING_PREFIX):
        return False
    parts = key[len(REPLACEMENT_STAGING_PREFIX):].split("/")
    if len(parts) != 3 or not parts[0]:
        return False
    try:
        return all(str(uuid.UUID(part)) == part for part in parts[1:])
    except ValueError:
        return False


async def _reconcile_staging_once() -> int:
    """Rediscover expired temporary bytes, including writes after cleanup.

    A page is advanced only after its DB work commits. Every full traversal
    wraps to the beginning, so recreated keys behind the cursor are not lost.
    Object age is a retention input, NOT proof that all writers have stopped.
    No database connection is held while listing the object store.
    """
    global _last_staging_scan_at, _staging_scan_cursor
    now = time.monotonic()
    if now - _last_staging_scan_at < STAGING_SCAN_INTERVAL_SECONDS:
        return 0
    _last_staging_scan_at = now
    objects, next_cursor = await asyncio.to_thread(
        s3_adapter.list_objects_page, REPLACEMENT_STAGING_PREFIX,
        continuation_token=_staging_scan_cursor, limit=STAGING_SCAN_PAGE_SIZE,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=REPLACEMENT_STAGING_RETENTION_SECONDS)
    keys = [obj["Key"] for obj in objects
            if _is_replacement_staging_key(obj["Key"])
            and isinstance(obj.get("LastModified"), datetime)
            and obj["LastModified"] < cutoff]
    queued = 0
    if keys:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for key in keys:
                    if not await vault_files_repo.try_lock_s3_key_for_mutation(conn, key):
                        continue
                    # Missing intents can be legitimate: expiry reaping and
                    # Vault deletion remove them. DB errors must propagate,
                    # never be interpreted as proof of expiry.
                    outbox_id = await conn.fetchval(
                        """
                        INSERT INTO s3_delete_outbox (s3_key, next_attempt_at)
                        SELECT $1::text, NOW()
                         WHERE NOT EXISTS (SELECT 1 FROM vault_files WHERE s3_key = $1)
                           AND NOT EXISTS (
                               SELECT 1 FROM m1_file_transfer_intents
                                WHERE id = $2 AND object_key = $1
                                  AND method = 'PUT' AND expires_at > NOW()
                           )
                           AND NOT EXISTS (
                               SELECT 1 FROM s3_delete_outbox
                                WHERE s3_key = $1 AND processed_at IS NULL
                           )
                        RETURNING id
                        """,
                        key, uuid.UUID(key.rsplit("/", 1)[1]),
                    )
                    queued += outbox_id is not None
    _staging_scan_cursor = next_cursor
    return queued


# ── Loop ─────────────────────────────────────────────────────────


async def _process_once() -> int:
    try:
        queued = await _reconcile_staging_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("s3_delete_worker staging inventory failed: %s", e)
        queued = 0
    try:
        d = await _process_deletes_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("s3_delete_worker delete pass failed: %s", e)
        d = 0
    try:
        await _sweep_outbox_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("s3_delete_worker outbox sweep failed: %s", e)
    return d + queued


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
                COUNT(*) FILTER (WHERE processed_at IS NULL AND abandoned_at IS NULL)     AS pending,
                COUNT(*) FILTER (WHERE processed_at IS NULL AND abandoned_at IS NOT NULL) AS abandoned
              FROM s3_delete_outbox
            """
        )
    return {
        "pending":   int(row["pending"]),
        "abandoned": int(row["abandoned"]),
    }
