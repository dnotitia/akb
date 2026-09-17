"""Real row-lock ordering and rollback of local-session revocation."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.db.postgres import _load_migration
from app.services import auth_service


@pytest.fixture
async def database(monkeypatch):
    dsn = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
    try:
        admin = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("REQUIRE_REAL_PG=1 but test PostgreSQL is unavailable")
        pytest.skip("Test PostgreSQL unavailable")
    schema = "session_gen_" + uuid.uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=3,
                                        server_settings={"search_path": schema})
        async with pool.acquire() as conn:
            await conn.execute("""CREATE TABLE users (
                id UUID PRIMARY KEY, username TEXT DEFAULT 'session-test',
                email TEXT DEFAULT 'session-test@example.test',
                display_name TEXT, is_admin BOOLEAN DEFAULT false,
                password_hash TEXT, auth_provider TEXT DEFAULT 'local',
                account_status TEXT DEFAULT 'active', account_kind TEXT DEFAULT 'human',
                credential_change_required BOOLEAN DEFAULT false,
                tokens_revoked_before TIMESTAMPTZ NOT NULL DEFAULT 'epoch',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
            migration = _load_migration("101_local_session_generation.py")
            await migration.migrate(conn=conn)
            await migration.migrate(conn=conn)
            user_id = uuid.uuid4()
            await conn.execute("INSERT INTO users(id) VALUES ($1)", user_id)
        monkeypatch.setattr(auth_service, "emit_event", AsyncMock())
        yield pool, user_id
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _revoke(conn, user_id):
    return await auth_service._revoke_sessions_in_conn(
        conn, user_id, actor_id=str(user_id), reason="self",
    )


async def test_old_transaction_revokes_newer_generation_and_cutoff_stays_monotonic(database):
    pool, user_id = database
    async with pool.acquire() as older, pool.acquire() as newer:
        async with older.transaction():
            await older.fetchval("SELECT now()")  # Establish an older transaction timestamp.
            async with newer.transaction():
                first = await _revoke(newer, user_id)
            second = await _revoke(older, user_id)
    assert second >= first
    assert await pool.fetchval("SELECT session_generation FROM users WHERE id=$1", user_id) == 2
    # A future historical cutoff cannot render generation-based new logins unusable.
    future = datetime.now(timezone.utc) + timedelta(days=1)
    await pool.execute("UPDATE users SET tokens_revoked_before=$2 WHERE id=$1", user_id, future)
    async with pool.acquire() as conn, conn.transaction():
        assert await _revoke(conn, user_id) == future


async def test_issuance_share_lock_serializes_revocation(database):
    pool, user_id = database
    async with pool.acquire() as issuer, pool.acquire() as revoker:
        transaction = issuer.transaction()
        await transaction.start()
        generation = await issuer.fetchval(
            "SELECT session_generation FROM users WHERE id=$1 FOR SHARE", user_id,
        )
        started = asyncio.Event()

        async def revoke():
            async with revoker.transaction():
                started.set()
                return await _revoke(revoker, user_id)

        pending = asyncio.create_task(revoke())
        try:
            await started.wait()
            # The server must report the revoker waiting for the issuer's row lock.
            for _ in range(100):
                waiting = await pool.fetchval(
                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=$1",
                    revoker.get_server_pid(),
                )
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting
            assert not pending.done()
        finally:
            await transaction.commit()
            await pending
    current = await pool.fetchval("SELECT session_generation FROM users WHERE id=$1", user_id)
    assert current == generation + 1
    assert not auth_service.local_session_generation_matches(
        {"iat": 9999999999, "session_generation": generation}, generation=current, revoked_epoch_ceil=0,
    )


async def test_audit_failure_rolls_back_generation_and_cutoff(database, monkeypatch):
    pool, user_id = database
    monkeypatch.setattr(auth_service, "emit_event", AsyncMock(side_effect=RuntimeError("audit failed")))
    before = await pool.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
    with pytest.raises(RuntimeError, match="audit failed"):
        async with pool.acquire() as conn, conn.transaction():
            await _revoke(conn, user_id)
    assert await pool.fetchrow("SELECT * FROM users WHERE id=$1", user_id) == before


@pytest.mark.parametrize("revoker", ["current", "legacy_revoke", "legacy_password_reset"])
async def test_rapid_login_revoke_and_password_change_use_current_generation(database, monkeypatch, tmp_path, revoker):
    from app.config import settings
    from app.services.local_session_keys import generate_local_session_keyset, clear_local_session_keyset_cache

    pool, user_id = database
    key_dir = tmp_path / "session-keys"
    generate_local_session_keyset(key_dir)
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "account_self_service_enabled", False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.test")
    monkeypatch.setattr(settings, "local_session_private_key_path", str(key_dir / "private.pem"))
    monkeypatch.setattr(settings, "local_session_jwks_path", str(key_dir / "jwks.json"))
    clear_local_session_keyset_cache()
    monkeypatch.setattr(auth_service, "get_pool", AsyncMock(return_value=pool))
    await pool.execute("UPDATE users SET password_hash=$2 WHERE id=$1", user_id,
                       await auth_service.hash_password_async("original-password"))
    tokens = []
    for generation in range(3):
        result = await auth_service.login("session-test", "original-password")
        token = result["token"]
        tokens.append(token)
        assert auth_service.decode_jwt(token)["session_generation"] == generation
        assert await auth_service._resolve_akb_session_jwt(token) is not None
        if revoker == "current":
            await auth_service.revoke_all_sessions(str(user_id))
        elif revoker == "legacy_revoke":
            # Exact legacy write shape: no generation awareness, feature off.
            await pool.execute("UPDATE users SET tokens_revoked_before=NOW(), updated_at=NOW() WHERE id=$1", user_id)
        else:
            await pool.execute("""UPDATE users SET password_hash=$2,
                tokens_revoked_before=NOW(), updated_at=NOW() WHERE id=$1""", user_id,
                await auth_service.hash_password_async("original-password"))
        for previous in tokens:
            assert await auth_service._resolve_akb_session_jwt(previous) is None
    before_change = (await auth_service.login("session-test", "original-password"))["token"]
    await auth_service.change_password(str(user_id), "original-password", "replacement-password")
    assert await auth_service._resolve_akb_session_jwt(before_change) is None
    after_change = (await auth_service.login("session-test", "replacement-password"))["token"]
    assert auth_service.decode_jwt(after_change)["session_generation"] == 4
    assert await auth_service._resolve_akb_session_jwt(after_change) is not None


async def test_legacy_cutoff_fence_handles_equal_backdated_and_rolled_back_writes(database):
    pool, user_id = database
    future = datetime.now(timezone.utc) + timedelta(days=1)
    await pool.execute("UPDATE users SET tokens_revoked_before=$2 WHERE id=$1", user_id, future)
    for generation, cutoff in [(2, future), (3, future - timedelta(days=2))]:
        await pool.execute("UPDATE users SET tokens_revoked_before=$2 WHERE id=$1", user_id, cutoff)
        row = await pool.fetchrow("SELECT session_generation,tokens_revoked_before FROM users WHERE id=$1", user_id)
        assert row["session_generation"] == generation
        assert row["tokens_revoked_before"] == future
        assert not auth_service.local_session_generation_matches(
            {"iat": 9999999999, "session_generation": generation - 1},
            generation=generation, revoked_epoch_ceil=0)
    await pool.execute("UPDATE users SET display_name='Profile edit' WHERE id=$1", user_id)
    assert await pool.fetchval("SELECT session_generation FROM users WHERE id=$1", user_id) == 3

    with pytest.raises(RuntimeError, match="rollback fixture"):
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE users SET tokens_revoked_before=NOW() WHERE id=$1", user_id)
            assert await conn.fetchval("SELECT session_generation FROM users WHERE id=$1", user_id) == 4
            raise RuntimeError("rollback fixture")
    assert await pool.fetchval("SELECT session_generation FROM users WHERE id=$1", user_id) == 3


async def test_init_before_migration_keeps_legacy_revoker_working():
    from tests.test_recovery_admin_provisioning_postgres import _fresh_database

    async with _fresh_database() as pool:
        async with pool.acquire() as conn:
            await conn.execute("DROP TRIGGER users_local_session_cutoff_fence ON users")
            await conn.execute("ALTER TABLE users DROP COLUMN session_generation")
            user_id = uuid.uuid4()
            await conn.execute("""INSERT INTO users(id,username,email,password_hash)
                VALUES($1,'upgrade-test','upgrade@example.test','unused-hash')""", user_id)
            init_sql = (Path(__file__).parents[1] / "app/db/init.sql").read_text()
            await conn.execute(init_sql)
            # init_db executes init.sql before applying pending migrations.
            # Old processes must still be able to revoke in that interval.
            await conn.execute("UPDATE users SET tokens_revoked_before=NOW() WHERE id=$1", user_id)
            migration = _load_migration("101_local_session_generation.py")
            await migration.migrate(conn=conn)
            await conn.execute("UPDATE users SET tokens_revoked_before=NOW() WHERE id=$1", user_id)
            assert await conn.fetchval("SELECT session_generation FROM users WHERE id=$1", user_id) == 1
            await conn.execute(init_sql)
            assert await conn.fetchval("SELECT session_generation FROM users WHERE id=$1", user_id) == 1
