"""Static guard for the SSO browser-session epoch boundary."""

import os
from pathlib import Path
import subprocess
import sys

import asyncpg
import pytest


_BACKEND = Path(__file__).resolve().parents[1]


def test_fresh_and_migrated_schema_share_the_custody_invariants():
    init_sql = (_BACKEND / "app" / "db" / "init.sql").read_text()
    custody_migration = (
        _BACKEND / "app" / "db" / "migrations" / "074_sso_browser_sessions.py"
    ).read_text()
    epoch_migration = (
        _BACKEND / "app" / "db" / "migrations" / "076_sso_session_epoch.py"
    ).read_text()

    for source in (init_sql, custody_migration):
        assert "CREATE TABLE IF NOT EXISTS sso_browser_sessions" in source
        assert "token_hash TEXT NOT NULL UNIQUE" in source
        assert "csrf_token_hash TEXT NOT NULL" in source
        assert "token_envelope TEXT NOT NULL" in source
        assert "FOREIGN KEY (external_identity_id, user_id)" in source
        assert "access_expires_at TIMESTAMPTZ NOT NULL" in source
        assert "refresh_expires_at TIMESTAMPTZ NOT NULL" in source
        assert "idle_expires_at TIMESTAMPTZ NOT NULL" in source
        assert "absolute_expires_at TIMESTAMPTZ NOT NULL" in source
        assert "identity_issuer, keycloak_sid" in source
        assert "identity_issuer, identity_subject" in source
        assert "CREATE TABLE IF NOT EXISTS sso_browser_logout_fences" in source
        assert "logout_issued_at TIMESTAMPTZ NOT NULL" in source
        assert "idx_sso_browser_logout_fences_expiry" in source

    assert "CREATE TABLE IF NOT EXISTS auth_runtime_state" in init_sql
    assert "runtime_generation BIGINT NOT NULL" in init_sql
    assert "sso_session_epoch UUID" in init_sql
    assert init_sql.count("session_epoch UUID,") >= 3
    assert "PRIMARY KEY (identity_issuer, keycloak_sid)" in init_sql
    assert init_sql.count("REFERENCES auth_runtime_state(sso_session_epoch)") == 3
    assert "CREATE TABLE IF NOT EXISTS auth_runtime_epoch_upgrade" in init_sql
    assert "runtime_generation_floor BIGINT NOT NULL" in init_sql

    for table in (
        "admin_browser_sessions",
        "sso_browser_sessions",
        "sso_browser_logout_fences",
    ):
        assert f'"{table}",' in epoch_migration
        assert f"LOCK TABLE {table}" in epoch_migration
    assert "ALTER TABLE {table}" in epoch_migration
    assert "ADD COLUMN IF NOT EXISTS session_epoch UUID" in epoch_migration
    assert "ALTER COLUMN session_epoch SET NOT NULL" not in epoch_migration
    assert "auth_runtime_state_sso_session_epoch_key" in epoch_migration
    assert "REFERENCES auth_runtime_state(sso_session_epoch)" in epoch_migration
    assert "CREATE TABLE IF NOT EXISTS auth_runtime_state" in epoch_migration
    assert "PRIMARY KEY (identity_issuer, keycloak_sid)" in epoch_migration
    assert "reject_legacy_sso_session_epoch" in epoch_migration
    assert "stop-the-world-v1" in epoch_migration


def test_runtime_epoch_preflight_is_executable_and_public_safe():
    preflight_path = _BACKEND / "scripts" / "sso_session_epoch_preflight.py"
    preflight = preflight_path.read_text()

    assert "prepare-upgrade" in preflight
    assert "prepare-rollback" in preflight
    assert "status" in preflight
    assert '"runtime_generation":' not in preflight
    assert '"sso_session_epoch":' not in preflight

    env = {**os.environ, "PYTHONPATH": ""}
    result = subprocess.run(
        [sys.executable, str(preflight_path), "--help"],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_epoch_bridge_has_named_migration_only_retirement_contract():
    contract = (_BACKEND.parent / "docs" / "sso" / "README.md").read_text()
    heading = "## SSO session epoch migration bridge retirement gate"

    assert heading in contract
    section = contract.split(heading, 1)[1].split("\n## ", 1)[0]
    normalized = " ".join(section.replace("`", "").split()).casefold()
    for required_condition in (
        "all supported AKB rollback artifacts are epoch-capable",
        "pre-epoch image rollback support is formally ended",
        "deployment and fleet inventory shows no pre-epoch artifact",
        "upgrade and rollback rehearsal receipts exist for every supported deployment profile",
    ):
        assert required_condition.casefold() in normalized
    for atomic_removal in (
        "sso_session_epoch_upgrade",
        "prepare-rollback",
        "rollback_ready",
        "legacy NULL trigger and state",
        "make every session_epoch column NOT NULL",
        "remove or replace the legacy bridge tests",
    ):
        assert atomic_removal.casefold() in normalized
    assert "migration-only compatibility bridge, not a permanent feature" in normalized
    assert "all three conditions are evidenced" in normalized


def test_browser_session_migration_is_registered():
    registry = (_BACKEND / "app" / "db" / "postgres.py").read_text()

    assert '"074_sso_browser_sessions.py"' in registry
    assert '"076_sso_session_epoch.py"' in registry


@pytest.mark.asyncio
async def test_migration_runner_retries_a_fully_rolled_back_deadlock(
    monkeypatch,
):
    from app.db import postgres

    calls: list[str] = []

    class Connection:
        async def execute(self, sql, *args):
            del args
            calls.append(sql)

    connection = Connection()

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback

    class Pool:
        def acquire(self):
            return Acquire()

    class Migration:
        attempts = 0

        async def migrate(self, *, conn):
            assert conn is connection
            self.attempts += 1
            if self.attempts == 1:
                raise asyncpg.DeadlockDetectedError("deadlock detected")

    async def no_sleep(delay):
        assert delay == 0

    migration = Migration()
    monkeypatch.setattr(postgres.asyncio, "sleep", no_sleep)
    await postgres._run_one_migration(
        Pool(),
        "076_sso_session_epoch.py",
        migration,
        retries=2,
        backoff=0,
    )

    assert migration.attempts == 2
    assert sum("INSERT INTO schema_migrations" in sql for sql in calls) == 1
