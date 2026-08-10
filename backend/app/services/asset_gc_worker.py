"""Bounded lifecycle worker for hidden document-image attachments.

The database row is deleted in the same transaction that enqueues its object
key in ``s3_delete_outbox``.  The existing S3 worker then performs the remote
delete with retries.  Row locks serialize this collector with document claims,
so an upload is either claimed by a successful document write or collected,
never both.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from app.config import settings
from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner
from app.services.s3_delete_worker import enqueue_delete

logger = logging.getLogger("akb.asset_gc_worker")

BATCH_SIZE = 50
REVISION_EXPIRE_BATCH_SIZE = 1_000


async def collect_once() -> int:
    """Remove one batch that has neither a live nor retained revision ref."""
    pool = await get_pool()
    unclaimed_ttl = timedelta(hours=settings.document_asset_unclaimed_ttl_hours)
    claimed_grace = timedelta(days=settings.document_asset_revision_retention_days)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Expired manifests are no longer an authorization source. Remove
            # one indexed, locked batch per pass so retention changes cannot
            # turn the worker into an unbounded full-table DELETE transaction.
            await conn.execute(
                """
                WITH expired AS (
                    SELECT ctid
                      FROM document_asset_revision_refs
                     WHERE retain_until <= NOW()
                     ORDER BY retain_until
                     LIMIT $1
                     FOR UPDATE SKIP LOCKED
                )
                DELETE FROM document_asset_revision_refs refs
                 USING expired
                 WHERE refs.ctid = expired.ctid
                """,
                REVISION_EXPIRE_BATCH_SIZE,
            )
            rows = await conn.fetch(
                """
                WITH candidates AS (
                    SELECT vf.id
                      FROM vault_files vf
                     WHERE vf.kind = 'attachment'
                       AND NOT EXISTS (
                            SELECT 1 FROM document_asset_refs live
                             WHERE live.asset_id = vf.id
                               AND live.vault_id = vf.vault_id
                       )
                       AND NOT EXISTS (
                            SELECT 1 FROM document_asset_revision_refs rev
                             WHERE rev.asset_id = vf.id
                               AND rev.vault_id = vf.vault_id
                               AND rev.retain_until > NOW()
                       )
                       AND (
                            (
                                vf.attachment_claimed_at IS NULL
                                AND vf.created_at < NOW() - $1::interval
                            )
                            OR (
                                vf.attachment_claimed_at IS NOT NULL
                                AND vf.updated_at < NOW() - $2::interval
                            )
                       )
                     ORDER BY vf.created_at, vf.id
                     LIMIT $3
                     FOR UPDATE SKIP LOCKED
                )
                DELETE FROM vault_files vf
                 USING candidates c
                 WHERE vf.id = c.id
                RETURNING vf.id, vf.s3_key
                """,
                unclaimed_ttl, claimed_grace, BATCH_SIZE,
            )
            for row in rows:
                await enqueue_delete(conn, row["s3_key"])

    count = len(rows)
    if count:
        logger.info("document asset GC enqueued %d object deletion(s)", count)
    return count


_runner = BackfillRunner(
    "asset_gc_worker",
    collect_once,
    idle_secs=settings.document_asset_gc_interval_secs,
)
start = _runner.start
stop = _runner.stop


async def pending_stats() -> dict[str, int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE kind = 'attachment' AND attachment_claimed_at IS NULL
                ) AS unclaimed,
                COUNT(*) FILTER (
                    WHERE kind = 'attachment' AND attachment_claimed_at IS NOT NULL
                ) AS claimed
              FROM vault_files
            """
        )
    return {"unclaimed": int(row["unclaimed"]), "claimed": int(row["claimed"])}
