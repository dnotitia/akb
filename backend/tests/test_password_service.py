"""Unit tests for password_service."""
from __future__ import annotations

import os
import re
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
    """Make password_service.get_pool() return our test pool."""
    from app.services import password_service as ps
    async def _fake() -> asyncpg.Pool:
        return pool
    monkeypatch.setattr(ps, "get_pool", _fake)


@pytest.fixture
async def user(pool):
    """Insert a throwaway user and yield (id, username)."""
    from app.services.auth_service import hash_password
    uid = uuid.uuid4()
    uname = f"pwsvc-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash) "
            "VALUES ($1, $2, $3, $4)",
            uid, uname, f"{uname}@t.dev", hash_password("orig-password"),
        )
    yield uid, uname
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await conn.execute("DELETE FROM events WHERE ref_id = $1", str(uid))


def test_generate_temp_password_format():
    from app.services.password_service import generate_temp_password
    pw = generate_temp_password()
    # 12 base64url chars grouped 4-4-4 with dashes = 14 chars total
    assert re.fullmatch(r"[A-Za-z0-9_-]{4}-[A-Za-z0-9_-]{4}-[A-Za-z0-9_-]{4}", pw), pw


def test_generate_temp_password_uniqueness():
    from app.services.password_service import generate_temp_password
    seen = {generate_temp_password() for _ in range(100)}
    assert len(seen) == 100


async def test_reset_password_updates_hash_and_emits_event(pool, user):
    from app.services.auth_service import verify_password
    from app.services.password_service import reset_password
    uid, uname = user

    temp, returned_username = await reset_password(
        username=uname, actor_id="admin-uuid", method="admin_ui",
    )
    assert returned_username == uname
    assert re.fullmatch(r"[A-Za-z0-9_-]{4}-[A-Za-z0-9_-]{4}-[A-Za-z0-9_-]{4}", temp)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash FROM users WHERE id = $1", uid,
        )
    assert verify_password(temp, row["password_hash"])
    assert not verify_password("orig-password", row["password_hash"])

    async with pool.acquire() as conn:
        ev = await conn.fetchrow(
            "SELECT kind, ref_type, ref_id, actor_id, payload::text AS payload "
            "FROM events WHERE ref_id = $1 ORDER BY id DESC LIMIT 1",
            str(uid),
        )
    assert ev is not None
    assert ev["kind"] == "auth.password_reset"
    assert ev["ref_type"] == "user"
    assert ev["actor_id"] == "admin-uuid"
    import json
    payload = json.loads(ev["payload"])
    assert payload["user_id"] == str(uid)
    assert payload["username"] == uname
    assert payload["method"] == "admin_ui"


async def test_reset_password_cli_method_records_null_actor(pool, user):
    from app.services.password_service import reset_password
    uid, uname = user
    await reset_password(username=uname, actor_id=None, method="cli")
    async with pool.acquire() as conn:
        ev = await conn.fetchrow(
            "SELECT actor_id, payload::text AS payload FROM events "
            "WHERE ref_id = $1 ORDER BY id DESC LIMIT 1",
            str(uid),
        )
    assert ev["actor_id"] is None
    import json
    assert json.loads(ev["payload"])["method"] == "cli"


async def test_reset_password_user_not_found(pool):
    from app.exceptions import NotFoundError
    from app.services.password_service import reset_password
    with pytest.raises(NotFoundError):
        await reset_password(
            username=f"does-not-exist-{uuid.uuid4().hex[:6]}",
            actor_id=None, method="cli",
        )
