"""Unit tests for auth_service.change_password."""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN)
        await conn.close()
        return True
    except Exception:
        return False


@pytest.fixture
async def pool():
    if not await _can_connect():
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def patch_get_pool(monkeypatch, pool):
    from app.services import auth_service as auth
    async def _fake() -> asyncpg.Pool:
        return pool
    monkeypatch.setattr(auth, "get_pool", _fake)


@pytest.fixture
async def user(pool):
    from app.services.auth_service import hash_password
    uid = uuid.uuid4()
    uname = f"chgpw-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash) "
            "VALUES ($1, $2, $3, $4)",
            uid, uname, f"{uname}@t.dev", hash_password("orig-12345"),
        )
    yield uid, uname
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await conn.execute("DELETE FROM events WHERE ref_id = $1", str(uid))


async def test_change_password_success(pool, user):
    from app.services.auth_service import change_password, verify_password
    uid, _ = user
    await change_password(str(uid), "orig-12345", "new-secret-67")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE id = $1", uid)
    assert verify_password("new-secret-67", row["password_hash"])
    assert not verify_password("orig-12345", row["password_hash"])


async def test_change_password_wrong_current_raises(user):
    from app.exceptions import AuthenticationError
    from app.services.auth_service import change_password
    uid, _ = user
    with pytest.raises(AuthenticationError):
        await change_password(str(uid), "WRONG-current", "new-secret-67")


async def test_change_password_too_short_raises(user):
    from app.services.auth_service import change_password, BadPasswordChange
    uid, _ = user
    with pytest.raises(BadPasswordChange):
        await change_password(str(uid), "orig-12345", "short")


async def test_change_password_same_as_current_raises(user):
    from app.services.auth_service import change_password, BadPasswordChange
    uid, _ = user
    with pytest.raises(BadPasswordChange):
        await change_password(str(uid), "orig-12345", "orig-12345")


async def test_change_password_user_not_found_raises(pool):
    from app.exceptions import NotFoundError
    from app.services.auth_service import change_password
    with pytest.raises(NotFoundError):
        await change_password(str(uuid.uuid4()), "any", "new-secret-67")


async def test_change_password_emits_event(pool, user):
    from app.services.auth_service import change_password
    uid, _ = user
    await change_password(str(uid), "orig-12345", "new-secret-67")
    async with pool.acquire() as conn:
        ev = await conn.fetchrow(
            "SELECT kind, ref_type, ref_id, actor_id "
            "FROM events WHERE ref_id = $1 ORDER BY id DESC LIMIT 1",
            str(uid),
        )
    assert ev is not None
    assert ev["kind"] == "auth.password_changed"
    assert ev["ref_type"] == "user"
    assert ev["actor_id"] == str(uid)
