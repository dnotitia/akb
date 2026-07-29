"""Migration 049: external-git quarantine state machine + rollout fence.

Stage 3 of the external-git hardening effort. Two things, both additive and
idempotent:

1. **Quarantine state machine columns.** `vault_external_git` gains a small,
   poller-owned state machine that the pre-hardening (migration-010) poller does
   not understand:

     * ``sync_state``      — 'pending_preflight' | 'active' | 'quarantined'
     * ``sync_state_reason`` — a fixed, secret-free reason code (never a URL/token)
     * ``sync_state_at``   — when the state last changed
     * ``poll_next_at``    — the NEW scheduler cursor (replaces ``next_attempt_at``)
     * ``poll_retry_count``— the NEW retry counter (replaces ``retry_count``)

   Every EXISTING row becomes ``pending_preflight`` (via the column default) so
   the hardened poller re-validates + credential-scrubs it before it is ever
   fetched again. Only a passing Layer-2 preflight promotes a row to ``active``.

2. **Rollout fence (mixed-version / rollback safety).** The
   OLD poller claims on ``next_attempt_at <= NOW() AND retry_count < 8`` and
   knows nothing about ``sync_state``. During a rolling deploy, a rollback, or an
   old pod restart, an un-hardened binary could otherwise keep fetching remotes
   without the new validation. So this migration fences the LEGACY columns into
   a state the old ``_claim_one`` treats as un-claimable:

     * every existing row → ``next_attempt_at = 'infinity'`` (``infinity <= NOW()``
       is always false) and ``retry_count = 8`` (``8 < 8`` is false) — either
       predicate alone already fences it; both together are belt-and-suspenders;
     * the legacy columns' DEFAULTs are flipped to the same fence values so a row
       INSERTed by any binary (including a rolled-back one, or the current
       ``VaultExternalGitRepository.create`` which omits them) is born fenced;
     * a ``CHECK (next_attempt_at = 'infinity' AND retry_count >= 8)`` makes the
       fence **DB-enforced, not just a one-time data flip**.
       Without it an old binary that claimed a row *just before* the migration
       (in-flight) could later run ``mark_success`` (``retry_count=0``, a finite
       ``next_attempt_at``) and UNFENCE the row, then re-claim it. With the CHECK,
       any such write to the legacy columns is a non-fence value → CHECK violation
       → the old transaction aborts → the fence cannot be lifted.

   The hardened poller never reads or writes the legacy columns; it schedules on
   ``poll_next_at`` / ``poll_retry_count`` / ``sync_state`` only, and
   ``VaultExternalGitRepository.create`` omits them (born fenced by the DEFAULT).
   So both write only fence-satisfying values — the CHECK is inert for the new
   binary and load-bearing only against an old one.

Also: clamps ``poll_interval_secs`` into the code-owned hard bounds [60, 86400]
and pins them with a CHECK, plus a ``sync_state`` domain CHECK. Both constraints
are introduced ``NOT VALID`` → offending rows corrected → ``VALIDATE`` so a
legacy value can never make the migration itself fail.

Idempotent — safe to re-run on fresh and existing DBs. The fence UPDATE only
touches the (now poller-ignored) legacy columns, never ``sync_state`` /
``poll_next_at``, so re-running never resets a row that preflight already
activated.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.049")

# The pre-hardening poller's MAX_RETRIES (== len(_backfill.BACKOFF_SECS)). This
# is FROZEN history: it is the old binary's claim bound, baked in here so the
# fence makes ``retry_count < 8`` false. The ``next_attempt_at='infinity'`` fence
# below is independent of this value, so the fence holds even if the live
# constant ever changes.
_OLD_POLLER_MAX_RETRIES = 8

# Code-owned hard bounds for the poll interval (fixed min/max, NEVER a
# runtime-config reference). These match the Settings defaults but are pinned as
# literals so the DB constraint cannot drift with operator config.
_POLL_INTERVAL_HARD_MIN = 60
_POLL_INTERVAL_HARD_MAX = 86400


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        # 1. Quarantine state-machine columns. The DEFAULTs initialize every
        #    EXISTING row: sync_state='pending_preflight' (re-validate before
        #    fetch), poll_next_at=NOW() (immediately preflight-due),
        #    poll_retry_count=0. Re-running is a no-op (IF NOT EXISTS), so an
        #    already-activated row keeps its state on a second apply.
        await conn.execute(
            """
            ALTER TABLE vault_external_git
                ADD COLUMN IF NOT EXISTS sync_state        TEXT NOT NULL
                    DEFAULT 'pending_preflight',
                ADD COLUMN IF NOT EXISTS sync_state_reason TEXT,
                ADD COLUMN IF NOT EXISTS sync_state_at     TIMESTAMPTZ NOT NULL
                    DEFAULT NOW(),
                ADD COLUMN IF NOT EXISTS poll_next_at      TIMESTAMPTZ NOT NULL
                    DEFAULT NOW(),
                ADD COLUMN IF NOT EXISTS poll_retry_count  INTEGER NOT NULL
                    DEFAULT 0
            """
        )

        # 2. Rollout fence for FUTURE inserts: flip the legacy
        #    scheduler columns' DEFAULTs so any row born from a binary that omits
        #    them — including a rolled-back old binary and the current
        #    repository create() — is un-claimable by the old poller.
        await conn.execute(
            "ALTER TABLE vault_external_git "
            "ALTER COLUMN next_attempt_at SET DEFAULT 'infinity'"
        )
        await conn.execute(
            f"ALTER TABLE vault_external_git "
            f"ALTER COLUMN retry_count SET DEFAULT {_OLD_POLLER_MAX_RETRIES}"
        )

        # 2b. Rollout-fence CHECK — make the fence
        #     DB-ENFORCED, not just a one-time data flip. Freeze the legacy
        #     scheduler columns at the fence for EVERY row so a rolled-back /
        #     mixed-version OLD poller that completes an in-flight op cannot
        #     UNFENCE its row: the old mark_success (retry_count=0 + finite
        #     next_attempt_at), mark_failure and claim-unfence all write
        #     non-fence values → CHECK violation → their transaction aborts. The
        #     hardened poller NEVER writes these columns and create() omits them
        #     (born fenced by the flipped DEFAULT), so the new binary only ever
        #     produces fence-satisfying values. Added NOT VALID here → step 3
        #     fences existing rows → step 3b VALIDATEs (safe order: a
        #     legacy value can never make the apply itself fail).
        await conn.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_external_git_rollout_fence'
                       AND conrelid = 'vault_external_git'::regclass
                ) THEN
                    ALTER TABLE vault_external_git
                        ADD CONSTRAINT vault_external_git_rollout_fence
                        CHECK (next_attempt_at = 'infinity'::timestamptz
                               AND retry_count >= {_OLD_POLLER_MAX_RETRIES})
                        NOT VALID;
                END IF;
            END
            $$
            """
        )

        # 3. Rollout fence for EXISTING rows. Touches ONLY the legacy columns the
        #    hardened poller ignores, so it is fully idempotent and never resets
        #    sync_state / poll_next_at (an activated row survives a re-apply).
        await conn.execute(
            f"""
            UPDATE vault_external_git
               SET next_attempt_at = 'infinity',
                   retry_count     = {_OLD_POLLER_MAX_RETRIES}
            """
        )

        # 3b. VALIDATE the rollout fence now that step 3 has fenced every existing
        #     row (all rows now satisfy the CHECK). Idempotent: VALIDATE on an
        #     already-validated constraint is a no-op.
        await conn.execute(
            "ALTER TABLE vault_external_git "
            "VALIDATE CONSTRAINT vault_external_git_rollout_fence"
        )

        # 4. Clamp poll_interval_secs into the hard bounds BEFORE adding its
        #    CHECK, so a legacy out-of-range value cannot make VALIDATE fail.
        #    Idempotent (no rows match once clamped).
        await conn.execute(
            f"""
            UPDATE vault_external_git
               SET poll_interval_secs = LEAST(
                       GREATEST(poll_interval_secs, {_POLL_INTERVAL_HARD_MIN}),
                       {_POLL_INTERVAL_HARD_MAX})
             WHERE poll_interval_secs < {_POLL_INTERVAL_HARD_MIN}
                OR poll_interval_secs > {_POLL_INTERVAL_HARD_MAX}
            """
        )

        # 5. sync_state domain CHECK. Rows are all 'pending_preflight' here, so
        #    there is nothing to correct; NOT VALID → VALIDATE anyway for a
        #    uniform, re-runnable path.
        await conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_external_git_sync_state_check'
                       AND conrelid = 'vault_external_git'::regclass
                ) THEN
                    ALTER TABLE vault_external_git
                        ADD CONSTRAINT vault_external_git_sync_state_check
                        CHECK (sync_state IN
                            ('pending_preflight', 'active', 'quarantined'))
                        NOT VALID;
                END IF;
            END
            $$
            """
        )
        await conn.execute(
            "ALTER TABLE vault_external_git "
            "VALIDATE CONSTRAINT vault_external_git_sync_state_check"
        )

        # 6. poll_interval_secs hard-bounds CHECK (literals, not config). Step 4
        #    already corrected every violator, so VALIDATE is guaranteed to pass.
        await conn.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'vault_external_git_poll_interval_bounds'
                       AND conrelid = 'vault_external_git'::regclass
                ) THEN
                    ALTER TABLE vault_external_git
                        ADD CONSTRAINT vault_external_git_poll_interval_bounds
                        CHECK (poll_interval_secs BETWEEN
                            {_POLL_INTERVAL_HARD_MIN} AND {_POLL_INTERVAL_HARD_MAX})
                        NOT VALID;
                END IF;
            END
            $$
            """
        )
        await conn.execute(
            "ALTER TABLE vault_external_git "
            "VALIDATE CONSTRAINT vault_external_git_poll_interval_bounds"
        )

        # 7. Claim index for the hardened poller's two claim paths (both filter
        #    on sync_state then order by poll_next_at). The legacy
        #    idx_vault_external_git_due (on next_attempt_at) is intentionally KEPT
        #    so an old poller stays fast during a mixed-version rollout.
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vault_external_git_claim
                ON vault_external_git (sync_state, poll_next_at)
            """
        )

    logger.info(
        "Migration 049 applied: vault_external_git quarantine state machine "
        "(sync_state/poll_next_at/poll_retry_count) + rollout fence "
        "(legacy next_attempt_at='infinity', retry_count=%d, DB-enforced by "
        "CHECK vault_external_git_rollout_fence) + hard-bound CHECKs",
        _OLD_POLLER_MAX_RETRIES,
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
