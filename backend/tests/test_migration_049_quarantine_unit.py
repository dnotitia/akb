"""DB-backed apply test for migration 049 (external_git quarantine + fence).

The rollout fence is the migration's subtlest guarantee: the
pre-hardening poller must be UNABLE to claim any row after the migration, across
a mixed-version rollout / rollback. This test proves it against a REAL Postgres
by replaying the OLD poller's verbatim claim SQL and asserting it returns
nothing, and by checking the new claim paths + CHECKs + idempotency on live
data.

Talks to a real Postgres via ``AKB_TEST_DSN`` (default the audit stack's
``localhost:5433``); skips when unreachable so the suite runs unattended. Runs
in a disposable database so it never touches a dev DB's data. The pure fence
LOGIC proof (no DB) lives in ``test_external_git_poller_unit.py``.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.services import external_git_poller as poller
from app.services._backfill import MAX_RETRIES

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")

# The pre-hardening poller's _claim_one, verbatim (migration-010 era). This is
# the exact query an OLD binary would run during a mixed-version rollout; the
# fence must make it claim ZERO rows.
_OLD_POLLER_CLAIM_SQL = """
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
"""

_PRE_049_SCHEMA = """
    CREATE TABLE vaults (
        id   UUID PRIMARY KEY,
        name TEXT NOT NULL
    );
    CREATE TABLE vault_external_git (
        vault_id           UUID PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
        remote_url         TEXT NOT NULL,
        remote_branch      TEXT NOT NULL DEFAULT 'main',
        auth_token         TEXT,
        poll_interval_secs INTEGER NOT NULL DEFAULT 300,
        last_synced_sha    TEXT,
        last_synced_at     TIMESTAMPTZ,
        last_error         TEXT,
        retry_count        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX idx_vault_external_git_due
        ON vault_external_git (next_attempt_at);
"""


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _load_migration_049():
    mig_path = (
        Path(__file__).resolve().parents[1]
        / "app" / "db" / "migrations" / "049_external_git_quarantine.py"
    )
    spec = importlib.util.spec_from_file_location("mig_049_under_test", mig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _insert_vault(conn, name: str, *, poll_interval: int, past_due: bool) -> uuid.UUID:
    vid = uuid.uuid4()
    await conn.execute("INSERT INTO vaults (id, name) VALUES ($1, $2)", vid, name)
    # Pre-049 legacy row: due (or not) under the OLD scheduler columns.
    next_at = "NOW() - INTERVAL '1 hour'" if past_due else "NOW() + INTERVAL '1 hour'"
    await conn.execute(
        f"""
        INSERT INTO vault_external_git
            (vault_id, remote_url, remote_branch, poll_interval_secs,
             retry_count, next_attempt_at)
        VALUES ($1, $2, 'main', $3, 0, {next_at})
        """,
        vid, "https://github.com/o/r.git", poll_interval,
    )
    return vid


@pytest.mark.asyncio
async def test_migration_049_fences_old_poller_and_builds_state_machine():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_mig049_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        base, _ = _DSN.rsplit("/", 1)
        conn = await asyncpg.connect(f"{base}/{dbname}")
        try:
            await conn.execute(_PRE_049_SCHEMA)
            # A legacy, due row with a sub-floor poll interval (to exercise the
            # [60, 86400] clamp).
            vid = await _insert_vault(conn, "legacy-mirror", poll_interval=30, past_due=True)

            mod = _load_migration_049()
            await mod.migrate(conn=conn)

            # ── new columns present ──────────────────────────────────
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'vault_external_git'"
                )
            }
            assert {
                "sync_state", "sync_state_reason", "sync_state_at",
                "poll_next_at", "poll_retry_count",
            } <= cols

            # ── the migrated row: pending_preflight, fenced, clamped ─
            row = await conn.fetchrow(
                """
                SELECT sync_state,
                       (next_attempt_at = 'infinity') AS at_inf,
                       retry_count,
                       poll_interval_secs,
                       (poll_next_at <= NOW())         AS preflight_due,
                       poll_retry_count
                  FROM vault_external_git WHERE vault_id = $1
                """,
                vid,
            )
            assert row["sync_state"] == "pending_preflight"
            assert row["at_inf"] is True          # existing-row legacy fence
            assert row["retry_count"] == 8        # existing-row legacy fence
            assert row["poll_interval_secs"] == 60  # 30 clamped up to the floor
            assert row["preflight_due"] is True   # immediately preflight-claimable
            assert row["poll_retry_count"] == 0

            # ── THE FENCE PROOF: the OLD poller claims nothing ───────
            async with conn.transaction():
                claimed = await conn.fetchrow(_OLD_POLLER_CLAIM_SQL, MAX_RETRIES, "3600")
            assert claimed is None

            # ── the NEW preflight claim DOES pick it up ──────────────
            async with conn.transaction():
                pf = await poller._claim_preflight(conn)
            assert pf is not None and pf["vault_id"] == vid

            # ── CHECK constraints exist AND are validated ────────────
            checks = {
                r["conname"]: r["convalidated"]
                for r in await conn.fetch(
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE conrelid = 'vault_external_git'::regclass AND contype = 'c'"
                )
            }
            assert checks.get("vault_external_git_sync_state_check") is True
            assert checks.get("vault_external_git_poll_interval_bounds") is True
            assert checks.get("vault_external_git_rollout_fence") is True

            # ── claim index built ────────────────────────────────────
            assert await conn.fetchval(
                "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_vault_external_git_claim'"
            )

            # ── CHECKs actually reject bad data ──────────────────────
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE vault_external_git SET sync_state = 'bogus' WHERE vault_id = $1",
                    vid,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE vault_external_git SET poll_interval_secs = 10 WHERE vault_id = $1",
                    vid,
                )

            # ── PROOF: the fence is DB-ENFORCED, not just a one-time flip ─────────
            # The OLD poller's mark_success would UNFENCE a row it claimed just
            # before the migration (retry_count=0 + a finite next_attempt_at).
            # The rollout-fence CHECK must REJECT that write, so a mixed-version
            # / rolled-back old binary can never lift the fence and re-claim.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    UPDATE vault_external_git
                       SET retry_count = 0,
                           next_attempt_at = NOW() + INTERVAL '300 seconds',
                           last_error = NULL
                     WHERE vault_id = $1
                    """,
                    vid,
                )
            # The rejected write rolled back — the row is still fenced.
            still = await conn.fetchrow(
                "SELECT (next_attempt_at = 'infinity') AS at_inf, retry_count "
                "FROM vault_external_git WHERE vault_id = $1",
                vid,
            )
            assert still["at_inf"] is True and still["retry_count"] == 8

            # The NEW binary is unaffected: a hardened write touching only the
            # poller-owned schedule columns (never the legacy fence) satisfies
            # the CHECK. (create() no-regression is covered below — a base-column
            # INSERT is born fenced by the DEFAULT and passes the CHECK.)
            await conn.execute(
                """
                UPDATE vault_external_git
                   SET poll_retry_count = 0,
                       poll_next_at = NOW(),
                       last_error = NULL
                 WHERE vault_id = $1
                """,
                vid,
            )

            # ── a NEW row (repo.create-style INSERT: base columns only) is
            #    born fenced by the flipped DEFAULTs ───────────────────
            nvid = uuid.uuid4()
            await conn.execute("INSERT INTO vaults (id, name) VALUES ($1, 'fresh')", nvid)
            await conn.execute(
                """
                INSERT INTO vault_external_git
                    (vault_id, remote_url, remote_branch, auth_token, poll_interval_secs)
                VALUES ($1, 'https://github.com/o/r2.git', 'main', NULL, 300)
                """,
                nvid,
            )
            fresh = await conn.fetchrow(
                """
                SELECT sync_state,
                       (next_attempt_at = 'infinity') AS at_inf,
                       retry_count,
                       (poll_next_at <= NOW())         AS preflight_due
                  FROM vault_external_git WHERE vault_id = $1
                """,
                nvid,
            )
            assert fresh["sync_state"] == "pending_preflight"
            assert fresh["at_inf"] is True      # future-insert fence (DEFAULT flip)
            assert fresh["retry_count"] == 8
            assert fresh["preflight_due"] is True
            # And the OLD poller still claims nothing with both rows present.
            async with conn.transaction():
                assert await conn.fetchrow(_OLD_POLLER_CLAIM_SQL, MAX_RETRIES, "3600") is None

            # ── idempotency: a re-apply must NOT reset an activated row ─
            await conn.execute(
                """
                UPDATE vault_external_git
                   SET sync_state = 'active', poll_retry_count = 3, poll_next_at = NOW()
                 WHERE vault_id = $1
                """,
                vid,
            )
            await mod.migrate(conn=conn)  # second run
            after = await conn.fetchrow(
                """
                SELECT sync_state, poll_retry_count,
                       (next_attempt_at = 'infinity') AS at_inf, retry_count
                  FROM vault_external_git WHERE vault_id = $1
                """,
                vid,
            )
            assert after["sync_state"] == "active"       # state machine preserved
            assert after["poll_retry_count"] == 3        # NOT reset by the fence UPDATE
            assert after["at_inf"] is True and after["retry_count"] == 8  # still fenced

            # ── the reconcile claim now picks up the active row ──────
            async with conn.transaction():
                rc = await poller._claim_reconcile(conn)
            assert rc is not None and rc["vault_id"] == vid

            # ── quarantine is a fully TERMINAL, IMMUTABLE state ──
            # Re-quarantining an already-quarantined row is a 0-row no-op that
            # PRESERVES the first reason — a stale re-quarantine (racing an
            # operator / converging peer) can never clobber the original reason.
            from app.repositories.vault_external_git_repo import (
                VaultExternalGitRepository,
            )
            pool = await asyncpg.create_pool(f"{base}/{dbname}", min_size=1, max_size=2)
            try:
                ext_repo = VaultExternalGitRepository(pool)
                qvid = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO vaults (id, name) VALUES ($1, 'quarantine-me')", qvid
                )
                await conn.execute(
                    """
                    INSERT INTO vault_external_git
                        (vault_id, remote_url, remote_branch, poll_interval_secs)
                    VALUES ($1, 'https://github.com/o/q.git', 'main', 300)
                    """,
                    qvid,
                )
                # pending_preflight → quarantined (reason A) transitions the row
                # (the preflight-path expected state is 'pending_preflight').
                assert await ext_repo.quarantine(
                    qvid, "policy_violation", "pending_preflight"
                ) is True
                # Re-quarantine with a DIFFERENT reason → 0-row no-op: the row is
                # now 'quarantined', which equals neither expected state.
                assert await ext_repo.quarantine(
                    qvid, "legacy_credential", "pending_preflight"
                ) is False
                assert await ext_repo.quarantine(
                    qvid, "legacy_credential", "active"
                ) is False
                kept = await conn.fetchrow(
                    "SELECT sync_state, sync_state_reason "
                    "FROM vault_external_git WHERE vault_id = $1",
                    qvid,
                )
                assert kept["sync_state"] == "quarantined"
                assert kept["sync_state_reason"] == "policy_violation"  # first reason kept
            finally:
                await pool.close()
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_scrub_legacy_credential_old_value_cas_on_live_pg():
    """DB-backed proof: the credential scrub is an
    OLD-VALUE CAS, so against a REAL Postgres it —

      (a) removes an embedded credential from ``remote_url`` even after the row
          was QUARANTINED (the CAS is state-independent, and a NULL token column
          matches a NULL claimed token via ``IS NOT DISTINCT FROM``), leaving
          ``sync_state`` / ``sync_state_reason`` untouched — no credential can
          linger in the terminal row;
      (b) is a 0-row no-op once the stored URL has changed under it, so a stale
          scrub never clobbers a newer operator config;
      (c) matches a non-NULL token only when the claimed token equals the stored
          one (``IS NOT DISTINCT FROM`` semantics).

    Skips when Postgres is unreachable so the suite still runs unattended."""
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    from app.repositories.vault_external_git_repo import VaultExternalGitRepository

    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_scrub_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        base, _ = _DSN.rsplit("/", 1)
        conn = await asyncpg.connect(f"{base}/{dbname}")
        try:
            await conn.execute(_PRE_049_SCHEMA)
            await _load_migration_049().migrate(conn=conn)
        finally:
            await conn.close()

        pool = await asyncpg.create_pool(f"{base}/{dbname}", min_size=1, max_size=2)
        try:
            ext_repo = VaultExternalGitRepository(pool)

            # ── (a) claim → concurrent quarantine → scrub ────────────
            vid = uuid.uuid4()
            raw_url = "https://x-access-token:ghp_secret@github.com/o/r.git"
            await pool.execute("INSERT INTO vaults (id, name) VALUES ($1, 'm')", vid)
            await pool.execute(
                "INSERT INTO vault_external_git "
                "(vault_id, remote_url, remote_branch, poll_interval_secs) "
                "VALUES ($1, $2, 'main', 300)",
                vid, raw_url,
            )
            # An operator/peer quarantines the row BEFORE the scrub runs (the row
            # is still 'pending_preflight' here, so that is the expected state).
            assert await ext_repo.quarantine(vid, "policy_violation", "pending_preflight") is True
            # The state-independent old-value CAS (claimed raw URL + NULL token)
            # still matches and scrubs the now-quarantined row.
            assert await ext_repo.scrub_legacy_credential(
                vid, "https://github.com/o/r.git", "ghp_secret", raw_url, None,
            ) is True
            row = await pool.fetchrow(
                "SELECT remote_url, auth_token, sync_state, sync_state_reason "
                "FROM vault_external_git WHERE vault_id = $1", vid,
            )
            assert row["remote_url"] == "https://github.com/o/r.git"  # userinfo gone
            assert "ghp_secret" not in row["remote_url"]
            assert row["auth_token"] == "ghp_secret"        # token lifted to column
            assert row["sync_state"] == "quarantined"        # state untouched
            assert row["sync_state_reason"] == "policy_violation"  # reason preserved

            # ── (b) 0-row no-clobber: re-running the now-STALE claim (the old
            #     URL no longer matches the scrubbed value) changes nothing ──
            assert await ext_repo.scrub_legacy_credential(
                vid, "https://attacker/x", None, raw_url, None,
            ) is False
            assert await pool.fetchval(
                "SELECT remote_url FROM vault_external_git WHERE vault_id = $1", vid,
            ) == "https://github.com/o/r.git"

            # ── (c) IS NOT DISTINCT FROM with a non-NULL stored token ────
            vid2 = uuid.uuid4()
            await pool.execute("INSERT INTO vaults (id, name) VALUES ($1, 'm2')", vid2)
            await pool.execute(
                "INSERT INTO vault_external_git (vault_id, remote_url, "
                "remote_branch, auth_token, poll_interval_secs) "
                "VALUES ($1, 'https://user:pass@h/r.git', 'main', 'col_tok', 300)",
                vid2,
            )
            # Wrong claimed token → no match, credential stays put (nothing scrubbed).
            assert await ext_repo.scrub_legacy_credential(
                vid2, "https://h/r.git", "col_tok", "https://user:pass@h/r.git", "WRONG",
            ) is False
            # Correct claimed token → scrub proceeds.
            assert await ext_repo.scrub_legacy_credential(
                vid2, "https://h/r.git", "col_tok", "https://user:pass@h/r.git", "col_tok",
            ) is True
            assert await pool.fetchval(
                "SELECT remote_url FROM vault_external_git WHERE vault_id = $1", vid2,
            ) == "https://h/r.git"
        finally:
            await pool.close()
    finally:
        await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_snapshot_cas_no_clobber_on_live_pg():
    """DB-backed proof: every preflight
    state transition is a snapshot-CAS on the exact ``(remote_url, auth_token)`` it
    validated, so an operator reconfigure mid-flight makes the stale transition a
    0-row no-op that leaves the new config intact — only the matching config
    transitions. Proven against a REAL Postgres across activate / mark_success /
    quarantine. Skips when Postgres is unreachable."""
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    from app.repositories.vault_external_git_repo import VaultExternalGitRepository

    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_snapcas_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        base, _ = _DSN.rsplit("/", 1)
        conn = await asyncpg.connect(f"{base}/{dbname}")
        try:
            await conn.execute(_PRE_049_SCHEMA)
            await _load_migration_049().migrate(conn=conn)
        finally:
            await conn.close()

        pool = await asyncpg.create_pool(f"{base}/{dbname}", min_size=1, max_size=2)
        try:
            ext_repo = VaultExternalGitRepository(pool)

            # ── activate: a stale (reconfigured) config → 0-row no-op ──
            vid = uuid.uuid4()
            await pool.execute("INSERT INTO vaults (id, name) VALUES ($1, 'a')", vid)
            await pool.execute(
                "INSERT INTO vault_external_git "
                "(vault_id, remote_url, remote_branch, poll_interval_secs) "
                "VALUES ($1, 'https://h/A.git', 'main', 300)",
                vid,
            )
            # Operator reconfigures to B (still pending) after we 'validated' A.
            await pool.execute(
                "UPDATE vault_external_git SET remote_url = 'https://h/B.git' "
                "WHERE vault_id = $1", vid,
            )
            # The stale activate guarded on the OLD (A) config matches 0 rows.
            assert await ext_repo.activate_from_preflight(
                vid, "https://h/A-canon.git", None, 300,
                validated_url="https://h/A.git", validated_token=None,
            ) is False
            row = await pool.fetchrow(
                "SELECT remote_url, sync_state FROM vault_external_git WHERE vault_id=$1",
                vid,
            )
            assert row["remote_url"] == "https://h/B.git"      # NOT clobbered
            assert row["sync_state"] == "pending_preflight"    # NOT activated
            # The matching (B) config DOES activate.
            assert await ext_repo.activate_from_preflight(
                vid, "https://h/B-canon.git", None, 300,
                validated_url="https://h/B.git", validated_token=None,
            ) is True
            row = await pool.fetchrow(
                "SELECT remote_url, sync_state FROM vault_external_git WHERE vault_id=$1",
                vid,
            )
            assert row["remote_url"] == "https://h/B-canon.git"
            assert row["sync_state"] == "active"

            # ── mark_success: a reconfigured active row → 0-row superseded ──
            await pool.execute(
                "UPDATE vault_external_git SET remote_url = 'https://h/C.git' "
                "WHERE vault_id = $1", vid,
            )
            assert await ext_repo.mark_success(
                vid, 300, new_sha="deadbeef",
                validated_url="https://h/B-canon.git", validated_token=None,
            ) is False
            assert await pool.fetchval(
                "SELECT last_synced_sha FROM vault_external_git WHERE vault_id=$1", vid,
            ) is None  # cursor NOT advanced for a config it never synced

            # ── quarantine: a reconfigured pending row → 0-row no-op ──
            qvid = uuid.uuid4()
            await pool.execute("INSERT INTO vaults (id, name) VALUES ($1, 'q')", qvid)
            await pool.execute(
                "INSERT INTO vault_external_git "
                "(vault_id, remote_url, remote_branch, poll_interval_secs) "
                "VALUES ($1, 'https://h/P.git', 'main', 300)",
                qvid,
            )
            await pool.execute(
                "UPDATE vault_external_git SET remote_url = 'https://h/P2.git' "
                "WHERE vault_id = $1", qvid,
            )
            assert await ext_repo.quarantine(
                qvid, "policy_violation", "pending_preflight",
                validated_url="https://h/P.git", validated_token=None,
            ) is False
            assert await pool.fetchval(
                "SELECT sync_state FROM vault_external_git WHERE vault_id=$1", qvid,
            ) == "pending_preflight"  # NOT terminalized on a stale validation
        finally:
            await pool.close()
    finally:
        await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_redact_malformed_and_quarantine_atomic_on_live_pg():
    """DB-backed proof: a malformed, credential-bearing URL
    is redacted (remote_url → sentinel, auth_token → NULL) AND quarantined in ONE
    atomic UPDATE, so the terminal row carries no secret in EITHER column. A
    reconfigure (old value gone) is a 0-row no-op that keeps the new config; an
    already-quarantined same-value row is hygiene-scrubbed while its FIRST reason is
    preserved. Skips when Postgres is unreachable."""
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    from app.repositories.vault_external_git_repo import (
        _MALFORMED_URL_SENTINEL,
        VaultExternalGitRepository,
    )

    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_redact_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        base, _ = _DSN.rsplit("/", 1)
        conn = await asyncpg.connect(f"{base}/{dbname}")
        try:
            await conn.execute(_PRE_049_SCHEMA)
            await _load_migration_049().migrate(conn=conn)
        finally:
            await conn.close()

        pool = await asyncpg.create_pool(f"{base}/{dbname}", min_size=1, max_size=2)
        try:
            ext_repo = VaultExternalGitRepository(pool)
            malformed = "https://x-access-token:ghp_secret@[::1"

            # ── (a) atomic redact + quarantine: no secret in either column ──
            vid = uuid.uuid4()
            await pool.execute("INSERT INTO vaults (id, name) VALUES ($1, 'm')", vid)
            await pool.execute(
                "INSERT INTO vault_external_git "
                "(vault_id, remote_url, remote_branch, auth_token, poll_interval_secs) "
                "VALUES ($1, $2, 'main', 'col_tok', 300)",
                vid, malformed,
            )
            assert await ext_repo.redact_malformed_and_quarantine(
                vid, malformed, "col_tok", "malformed_url",
            ) is True
            row = await pool.fetchrow(
                "SELECT remote_url, auth_token, sync_state, sync_state_reason "
                "FROM vault_external_git WHERE vault_id=$1", vid,
            )
            assert row["remote_url"] == _MALFORMED_URL_SENTINEL   # redacted
            assert "ghp_secret" not in row["remote_url"]
            assert row["auth_token"] is None                      # token nulled
            assert row["sync_state"] == "quarantined"             # terminal
            assert row["sync_state_reason"] == "malformed_url"

            # ── (b) 0-row no-op once the stored value changed under a stale claim ──
            vid2 = uuid.uuid4()
            await pool.execute("INSERT INTO vaults (id, name) VALUES ($1, 'm2')", vid2)
            await pool.execute(
                "INSERT INTO vault_external_git "
                "(vault_id, remote_url, remote_branch, poll_interval_secs) "
                "VALUES ($1, $2, 'main', 300)",
                vid2, malformed,
            )
            # Operator reconfigures to a clean URL before the redact runs.
            await pool.execute(
                "UPDATE vault_external_git SET remote_url = 'https://github.com/o/r.git' "
                "WHERE vault_id = $1", vid2,
            )
            assert await ext_repo.redact_malformed_and_quarantine(
                vid2, malformed, None, "malformed_url",
            ) is False
            row = await pool.fetchrow(
                "SELECT remote_url, sync_state FROM vault_external_git WHERE vault_id=$1",
                vid2,
            )
            assert row["remote_url"] == "https://github.com/o/r.git"  # new config kept
            assert row["sync_state"] == "pending_preflight"           # NOT quarantined

            # ── (c) hygiene on an already-quarantined same-value row keeps reason ──
            vid3 = uuid.uuid4()
            await pool.execute("INSERT INTO vaults (id, name) VALUES ($1, 'm3')", vid3)
            await pool.execute(
                "INSERT INTO vault_external_git "
                "(vault_id, remote_url, remote_branch, poll_interval_secs) "
                "VALUES ($1, $2, 'main', 300)",
                vid3, malformed,
            )
            # A peer quarantined for another reason but left the credential in place.
            await pool.execute(
                "UPDATE vault_external_git "
                "SET sync_state='quarantined', sync_state_reason='policy_violation', "
                "    sync_state_at = NOW() "
                "WHERE vault_id = $1", vid3,
            )
            assert await ext_repo.redact_malformed_url_if_quarantined(
                vid3, malformed, None,
            ) is True
            row = await pool.fetchrow(
                "SELECT remote_url, auth_token, sync_state, sync_state_reason "
                "FROM vault_external_git WHERE vault_id=$1", vid3,
            )
            assert row["remote_url"] == _MALFORMED_URL_SENTINEL   # credential gone
            assert row["auth_token"] is None
            assert row["sync_state"] == "quarantined"
            assert row["sync_state_reason"] == "policy_violation"  # FIRST reason kept
        finally:
            await pool.close()
    finally:
        await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_reconcile_quarantine_exact_expected_state_cas_on_live_pg():
    """DB-backed proof: ``quarantine``
    is a CAS on the caller's EXACT expected ``sync_state``, so a stale
    reconcile-path policy quarantine (which expects 'active') cannot terminalize a
    row an operator RESET to 'pending_preflight' with a NEW config mid-reconcile —
    it is a 0-row no-op that leaves the operator's fresh config intact. Proven with
    TWO connections against a REAL Postgres — a reconcile *worker* that claimed the
    row while it was active, and an *operator* that reconfigures it — so the
    temporal race is genuine, not a single-connection simulation. Also pins that
    the RIGHT caller still works (a preflight-path quarantine expecting
    'pending_preflight' terminalizes), that an already-quarantined row is immutable
    under EITHER expected state, and that a still-active row is terminalized by the
    reconcile path (no over-fencing). Skips when Postgres is unreachable."""
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    from app.repositories.vault_external_git_repo import VaultExternalGitRepository

    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_qexp_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        base, _ = _DSN.rsplit("/", 1)
        conn = await asyncpg.connect(f"{base}/{dbname}")
        try:
            await conn.execute(_PRE_049_SCHEMA)
            await _load_migration_049().migrate(conn=conn)
        finally:
            await conn.close()

        # Two independent connections model the real race: the reconcile worker and
        # the operator each hold their own session and commit independently.
        worker = await asyncpg.connect(f"{base}/{dbname}")
        operator = await asyncpg.connect(f"{base}/{dbname}")
        pool = await asyncpg.create_pool(f"{base}/{dbname}", min_size=1, max_size=2)
        try:
            ext_repo = VaultExternalGitRepository(pool)

            # ── an ACTIVE mirror the worker is about to reconcile ──
            vid = uuid.uuid4()
            await operator.execute("INSERT INTO vaults (id, name) VALUES ($1, 'm')", vid)
            await operator.execute(
                "INSERT INTO vault_external_git "
                "(vault_id, remote_url, remote_branch, auth_token, poll_interval_secs, "
                " sync_state) "
                "VALUES ($1, 'https://h/OLD.git', 'main', 'old_tok', 300, 'active')",
                vid,
            )
            # The worker claims it while active (exactly as _run_reconcile's claim),
            # commits the claim, and goes off to do network work.
            async with worker.transaction():
                claimed = await poller._claim_reconcile(worker)
            assert claimed is not None and claimed["vault_id"] == vid

            # ── the 3b reset: mid-reconcile the operator reconfigures the mirror and
            #    resets it to pending_preflight with a NEW url/token ──
            await operator.execute(
                """
                UPDATE vault_external_git
                   SET remote_url        = 'https://h/NEW.git',
                       auth_token        = 'new_tok',
                       sync_state        = 'pending_preflight',
                       sync_state_reason = NULL,
                       sync_state_at     = NOW(),
                       poll_next_at      = NOW()
                 WHERE vault_id = $1
                """,
                vid,
            )
            before = await operator.fetchrow(
                "SELECT remote_url, auth_token, sync_state, sync_state_reason, "
                "       sync_state_at "
                "FROM vault_external_git WHERE vault_id = $1", vid,
            )

            # ── the stale reconcile-path quarantine expects 'active' (state-only —
            #    the reconcile path carries no old-value snapshot). The row is now
            #    'pending_preflight', so this is a 0-row no-op: SUPERSEDED ──
            assert await ext_repo.quarantine(vid, "policy_violation", "active") is False
            after = await operator.fetchrow(
                "SELECT remote_url, auth_token, sync_state, sync_state_reason, "
                "       sync_state_at "
                "FROM vault_external_git WHERE vault_id = $1", vid,
            )
            # The operator's NEW config is intact: url, token, state, reason, AND the
            # timestamp are all unchanged by the superseded quarantine.
            assert after["remote_url"] == "https://h/NEW.git"
            assert after["auth_token"] == "new_tok"
            assert after["sync_state"] == "pending_preflight"
            assert after["sync_state_reason"] is None
            assert after["sync_state_at"] == before["sync_state_at"]

            # ── the RIGHT caller still works: a preflight-path quarantine expecting
            #    'pending_preflight' DOES terminalize the reset row (no over-fencing) ──
            assert await ext_repo.quarantine(
                vid, "policy_violation", "pending_preflight"
            ) is True
            term = await operator.fetchrow(
                "SELECT sync_state, sync_state_reason "
                "FROM vault_external_git WHERE vault_id = $1", vid,
            )
            assert term["sync_state"] == "quarantined"
            assert term["sync_state_reason"] == "policy_violation"

            # ── immutability: an already-quarantined row equals NEITHER expected
            #    state, so a re-quarantine from either caller is a 0-row no-op that
            #    preserves the FIRST reason ──
            assert await ext_repo.quarantine(vid, "legacy_credential", "active") is False
            assert await ext_repo.quarantine(
                vid, "legacy_credential", "pending_preflight"
            ) is False
            assert await operator.fetchval(
                "SELECT sync_state_reason FROM vault_external_git WHERE vault_id = $1", vid,
            ) == "policy_violation"  # first reason preserved

            # ── no-regression: a STILL-active row IS terminalized by the reconcile
            #    path's quarantine(expected='active') ──
            avid = uuid.uuid4()
            await operator.execute("INSERT INTO vaults (id, name) VALUES ($1, 'a')", avid)
            await operator.execute(
                "INSERT INTO vault_external_git "
                "(vault_id, remote_url, remote_branch, poll_interval_secs, sync_state) "
                "VALUES ($1, 'https://h/A.git', 'main', 300, 'active')",
                avid,
            )
            assert await ext_repo.quarantine(avid, "policy_violation", "active") is True
            arow = await operator.fetchrow(
                "SELECT sync_state, sync_state_reason "
                "FROM vault_external_git WHERE vault_id = $1", avid,
            )
            assert arow["sync_state"] == "quarantined"
            assert arow["sync_state_reason"] == "policy_violation"
        finally:
            await worker.close()
            await operator.close()
            await pool.close()
    finally:
        await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        await admin.close()
