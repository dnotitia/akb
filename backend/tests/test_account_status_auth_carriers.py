"""Suspension must deny every AKB-owned authentication carrier."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from app.config import settings
from app.exceptions import AccountSuspendedError


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


@pytest.fixture
async def pool(monkeypatch):
    try:
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    from app.db.postgres import _load_migration
    from app.services import auth_service

    migration = _load_migration("043_workspace_account_governance.py")
    assert migration is not None
    async with pool.acquire() as conn:
        await migration.migrate(conn=conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(settings, "local_auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "jwt_secret", "status-test-secret-at-least-32-bytes", raising=False)
    yield pool

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username LIKE 'status-%'")
    await pool.close()


async def _create_user(pool, *, status: str = "active") -> tuple[uuid.UUID, str]:
    from app.services.auth_service import hash_password

    user_id = uuid.uuid4()
    username = f"status-{uuid.uuid4().hex[:12]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, account_status, account_kind
            ) VALUES ($1, $2, $3, $4, $5, 'human')
            """,
            user_id,
            username,
            f"{username}@example.com",
            hash_password("known-password"),
            status,
        )
    return user_id, username


async def test_suspended_local_user_cannot_login(pool):
    from app.services.auth_service import login

    _, username = await _create_user(pool, status="suspended")
    with pytest.raises(AccountSuspendedError):
        await login(username, "known-password")


async def test_existing_session_jwt_fails_after_suspension(pool):
    from app.services.auth_service import create_jwt, resolve_token

    user_id, username = await _create_user(pool)
    token = create_jwt(str(user_id), username)
    assert await resolve_token(f"Bearer {token}") is not None

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET account_status = 'suspended' WHERE id = $1",
            user_id,
        )
    assert await resolve_token(f"Bearer {token}") is None


@pytest.mark.parametrize("key_class", ["pat", "service", "publishable"])
async def test_every_token_key_class_fails_after_suspension(pool, key_class):
    from app.services.auth_service import _hash_token, resolve_token

    user_id, _ = await _create_user(pool)
    raw_token = f"akb_status_{key_class}_{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        token_id = await conn.fetchval(
            """
            INSERT INTO tokens (
                user_id, name, token_hash, token_prefix, key_class
            ) VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            user_id,
            f"status-{key_class}",
            _hash_token(raw_token),
            raw_token[:12],
            key_class,
        )
        await conn.execute(
            "UPDATE users SET account_status = 'suspended' WHERE id = $1",
            user_id,
        )

    assert await resolve_token(f"Bearer {raw_token}") is None
    async with pool.acquire() as conn:
        last_used_at = await conn.fetchval(
            "SELECT last_used_at FROM tokens WHERE id = $1",
            token_id,
        )
    assert last_used_at is None


async def test_suspended_user_cannot_receive_a_new_pat(pool):
    from app.services.auth_service import create_pat

    user_id, _ = await _create_user(pool, status="suspended")
    with pytest.raises(AccountSuspendedError):
        await create_pat(str(user_id), "must-not-exist")

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM tokens WHERE user_id = $1",
            user_id,
        )
    assert count == 0


async def test_suspended_user_cannot_change_password_via_internal_service(pool):
    from app.services.auth_service import change_password, verify_password

    user_id, _ = await _create_user(pool, status="suspended")
    with pytest.raises(AccountSuspendedError):
        await change_password(str(user_id), "known-password", "new-known-password")

    async with pool.acquire() as conn:
        password_hash = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            user_id,
        )
    assert verify_password("known-password", password_hash)


async def test_local_login_waits_for_inflight_suspension_and_then_denies(pool):
    from app.services.auth_service import login

    user_id, username = await _create_user(pool)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT id FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            await conn.execute(
                "UPDATE users SET account_status = 'suspended' WHERE id = $1",
                user_id,
            )
            login_task = asyncio.create_task(login(username, "known-password"))
            await asyncio.sleep(0.05)
            assert not login_task.done()

    with pytest.raises(AccountSuspendedError):
        await login_task


async def test_jwt_resolution_waits_for_inflight_suspension_and_then_denies(pool):
    from app.services.auth_service import create_jwt, resolve_token

    user_id, username = await _create_user(pool)
    token = create_jwt(str(user_id), username)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT id FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            await conn.execute(
                "UPDATE users SET account_status = 'suspended' WHERE id = $1",
                user_id,
            )
            resolve_task = asyncio.create_task(resolve_token(f"Bearer {token}"))
            await asyncio.sleep(0.05)
            assert not resolve_task.done()

    assert await resolve_task is None


async def test_active_local_user_keeps_existing_profile_edit_behavior(pool):
    from app.services.auth_service import update_profile

    user_id, _ = await _create_user(pool)
    email = f"status-updated-{uuid.uuid4().hex[:10]}@example.com"
    result = await update_profile(
        str(user_id),
        email=email,
        display_name="Updated local user",
    )

    assert result["email"] == email
    assert result["display_name"] == "Updated local user"
