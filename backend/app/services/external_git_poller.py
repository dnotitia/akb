"""Background poller for external_git mirror vaults.

Drains `vault_external_git` rows whose `next_attempt_at` has come due,
runs one `ExternalGitService.reconcile` per claim, and lets the repo's
`mark_success` / `mark_failure` methods reset the cursor.

Loop mechanics (start/stop, idle cadence) come from `_backfill`. Backoff
on hard failures uses the shared schedule (60s → 6h, capped at 8 retries
before manual intervention).

Each iteration claims a single vault. Reconcile is heavy (network +
git fetch + per-file DB writes) so batching many vaults into one
iteration would waste the SKIP LOCKED window for nothing — the loop
will simply spin again immediately when work remains.
"""

from __future__ import annotations

import logging

from app.db.postgres import get_pool
from app.repositories.vault_external_git_repo import VaultExternalGitRepository
from app.services._backfill import BackfillRunner, MAX_RETRIES, next_attempt_delay
from app.services.external_git_service import ExternalGitService

logger = logging.getLogger("akb.external_git_poller")


_service = ExternalGitService()


async def _claim_one(conn) -> dict | None:
    """Claim the most-due mirror vault. Pushes next_attempt_at forward
    by `settings.external_git_claim_lookahead_secs` so peer workers (or
    the same worker on its next pass) skip this row while reconcile is
    in flight. The interval has to exceed the longest realistic initial
    bootstrap — a 1GB mirror clone can easily run past the default 10m."""
    from app.config import settings  # local import to dodge circular import
    row = await conn.fetchrow(
        """
        WITH due AS (
            SELECT veg.vault_id, v.name AS vault_name
              FROM vault_external_git veg
              JOIN vaults v ON v.id = veg.vault_id
             WHERE veg.next_attempt_at <= NOW()
               AND veg.retry_count < $1
             ORDER BY veg.next_attempt_at
             LIMIT 1
             FOR UPDATE OF veg SKIP LOCKED
        )
        UPDATE vault_external_git veg
           SET next_attempt_at = NOW() + ($2 || ' seconds')::interval
          FROM due
         WHERE veg.vault_id = due.vault_id
        RETURNING veg.vault_id, due.vault_name, veg.retry_count
        """,
        MAX_RETRIES, str(settings.external_git_claim_lookahead_secs),
    )
    return dict(row) if row else None


async def _process_once() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            claim = await _claim_one(conn)

    if claim is None:
        return 0

    vault_id = claim["vault_id"]
    vault_name = claim["vault_name"]
    retry_count = claim["retry_count"]

    ext_repo = VaultExternalGitRepository(pool)
    try:
        await _service.reconcile(vault_id, vault_name)
        return 1
    except Exception as e:  # noqa: BLE001 — keep loop alive
        delay = next_attempt_delay(retry_count)
        logger.warning(
            "External sync failed: vault=%s retry=%d backoff=%ds err=%s",
            vault_name, retry_count, delay, e,
        )
        await ext_repo.mark_failure(vault_id, str(e), delay)
        return 0


_runner = BackfillRunner("external_git_poller", _process_once)
start = _runner.start
stop = _runner.stop


async def pending_stats() -> dict:
    """Snapshot for /health."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                                    AS total,
                COUNT(*) FILTER (WHERE next_attempt_at <= NOW())            AS due,
                COUNT(*) FILTER (WHERE retry_count > 0
                                 AND retry_count < $1)                      AS retrying,
                COUNT(*) FILTER (WHERE retry_count >= $1)                   AS abandoned
              FROM vault_external_git
            """,
            MAX_RETRIES,
        )
    return {
        "total":     int(row["total"]),
        "due":       int(row["due"]),
        "retrying":  int(row["retrying"]),
        "abandoned": int(row["abandoned"]),
    }
