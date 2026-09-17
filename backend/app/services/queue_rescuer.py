"""Close durable queue claims that died on their final attempt.

Workers stamp terminal state in their ordinary exception path.  A SIGKILL,
OOM, or host loss has no exception path, though, so a claim whose counter was
incremented to ``MAX_RETRIES`` would otherwise remain an ambiguous lease
forever.  This singleton rescuer waits for the lease to expire and then stamps
the queue's explicit terminal state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner, MAX_RETRIES

logger = logging.getLogger("akb.queue_rescuer")

_ADVISORY_LOCK_KEY = 0x414B425F51524553  # ``AKB_QRES``
_last_run_at: datetime | None = None
_last_rescued = 0
_last_error: str | None = None

_RESCUE_STATEMENTS = (
    """
    WITH rescued AS (
        UPDATE chunks
           SET vector_abandoned_at = NOW(), vector_claimed_at = NULL,
               vector_next_attempt_at = NULL,
               vector_last_error = COALESCE(
                   vector_last_error, 'claim lease expired after final attempt'
               )
         WHERE vector_indexed_at IS NULL
           AND vector_abandoned_at IS NULL
           AND vector_retry_count >= $1
           AND (vector_next_attempt_at IS NULL OR vector_next_attempt_at <= NOW())
        RETURNING 1
    ) SELECT COUNT(*) FROM rescued
    """,
    """
    WITH rescued AS (
        UPDATE vector_delete_outbox
           SET abandoned_at = NOW(), claimed_at = NULL, next_attempt_at = NULL,
               last_error = COALESCE(
                   last_error, 'claim lease expired after final attempt'
               )
         WHERE processed_at IS NULL AND abandoned_at IS NULL
           AND retry_count >= $1
           AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
        RETURNING 1
    ) SELECT COUNT(*) FROM rescued
    """,
    """
    WITH rescued AS (
        UPDATE s3_delete_outbox
           SET abandoned_at = NOW(), claimed_at = NULL, next_attempt_at = NULL,
               last_error = COALESCE(
                   last_error, 'claim lease expired after final attempt'
               )
         WHERE processed_at IS NULL AND abandoned_at IS NULL
           AND retry_count >= $1
           AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
        RETURNING 1
    ) SELECT COUNT(*) FROM rescued
    """,
    """
    WITH rescued AS (
        UPDATE events
           SET abandoned_at = NOW(), claimed_at = NULL, next_attempt_at = NULL,
               last_error = COALESCE(
                   last_error, 'claim lease expired after final attempt'
               )
         WHERE redis_published_at IS NULL AND abandoned_at IS NULL
           AND attempts >= $1
           AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
        RETURNING 1
    ) SELECT COUNT(*) FROM rescued
    """,
    """
    WITH rescued AS (
        UPDATE documents
           SET llm_abandoned_at = NOW(), llm_claimed_at = NULL,
               llm_next_attempt_at = NULL,
               llm_last_error = COALESCE(
                   llm_last_error, 'claim lease expired after final attempt'
               )
         WHERE source = 'external_git' AND llm_metadata_at IS NULL
           AND llm_abandoned_at IS NULL AND llm_retry_count >= $1
           AND (llm_next_attempt_at IS NULL OR llm_next_attempt_at <= NOW())
        RETURNING 1
    ) SELECT COUNT(*) FROM rescued
    """,
    """
    WITH rescued AS (
        UPDATE native_invalidation_intents
           SET completed_at = NOW(), delivery_outcome = 'abandoned',
               claimed_at = NULL, next_attempt_at = NULL,
               last_error = COALESCE(
                   last_error, 'claim lease expired after final attempt'
               )
         WHERE completed_at IS NULL AND retry_count >= $1
           AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
        RETURNING 1
    ) SELECT COUNT(*) FROM rescued
    """,
    """
    WITH rescued AS (
        UPDATE native_file_projection_outbox
           SET completed_at = NOW(), outcome = 'abandoned',
               claimed_at = NULL, next_attempt_at = NULL,
               last_error = COALESCE(
                   last_error, 'claim lease expired after final attempt'
               )
         WHERE completed_at IS NULL AND retry_count >= $1
           AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
        RETURNING 1
    ) SELECT COUNT(*) FROM rescued
    """,
)


async def _rescue_once() -> int:
    global _last_run_at, _last_rescued, _last_error
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                leader = await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1)",
                    _ADVISORY_LOCK_KEY,
                )
                if not leader:
                    return 0
                rescued = 0
                for statement in _RESCUE_STATEMENTS:
                    rescued += int(await conn.fetchval(statement, MAX_RETRIES) or 0)
        _last_run_at = datetime.now(timezone.utc)
        _last_rescued = rescued
        _last_error = None
        if rescued:
            logger.warning("rescued %d expired final worker claim(s)", rescued)
        return rescued
    except Exception as exc:
        _last_error = str(exc)[:500]
        raise


def snapshot() -> dict:
    return {
        "last_run_at": _last_run_at.isoformat() if _last_run_at else None,
        "last_rescued": _last_rescued,
        "last_error": _last_error,
    }


_runner = BackfillRunner("queue_rescuer", _rescue_once, idle_secs=60)
start = _runner.start
stop = _runner.stop
