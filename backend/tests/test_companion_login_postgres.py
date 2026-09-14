"""Signed BFF completion reuses PR531 account policy against isolated PostgreSQL."""
import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.exceptions import AuthenticationError, AccountSuspendedError, MembershipRequiredError
from app.services import auth_service
from app.services import companion_login as service
from tests import test_companion_login_unit as proof_tests
from tests import test_authoritative_email_domains_postgres as authority_tests

login = proof_tests.login
pool = authority_tests.pool
_insert_unbound_user = authority_tests._insert_unbound_user
_REAL_CONSUME = service._consume_assertion
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def completion(pool, login, monkeypatch):
    from app.db.postgres import _load_migration
    async with pool.acquire() as conn:
        await _load_migration("101_companion_login.py").migrate(conn)
        await conn.execute("TRUNCATE companion_login_assertions")
    monkeypatch.setattr(service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(service, "_consume_assertion", _REAL_CONSUME)
    monkeypatch.setattr(auth_service, "get_role_sync", lambda: type("Sync", (), {"on_user_create": AsyncMock()})())
    return login


async def test_adoption_preserves_user_and_replay_is_rejected(pool, completion):
    req, tokens, sign, _, subject = completion
    uid = await _insert_unbound_user(pool, f"{subject}@corp.example")
    access, identity = tokens()
    assertion = sign(access, identity)
    result = await service.complete_companion_login(req, access, identity, assertion)
    assert result["user"]["id"] == str(uid)
    with pytest.raises(AuthenticationError, match="already used"):
        await service.complete_companion_login(req, access, identity, assertion)
    # A fresh authenticated request resolves the existing identity through
    # current account policy; no stored completion result is reused.
    assert await service.complete_companion_login(req, access, identity, sign(access, identity)) == result
    async with pool.acquire() as conn:
        records = await conn.fetch("SELECT * FROM companion_login_assertions")
        assert all(access not in str(dict(row)) and identity not in str(dict(row)) for row in records)
        await conn.execute("UPDATE users SET account_status='suspended' WHERE id=$1", uid)
    with pytest.raises(AccountSuspendedError):
        await service.complete_companion_login(req, access, identity, sign(access, identity))


async def test_concurrent_completions_one_account(pool, completion):
    req, tokens, sign, _, subject = completion
    access, identity = tokens()
    async def complete():
        return await service.complete_companion_login(req, access, identity, sign(access, identity))
    results = await asyncio.gather(complete(), complete())
    assert results[0] == results[1]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM external_identities WHERE subject=$1", subject) == 1
        assert await conn.fetchval("SELECT COUNT(*) FROM users WHERE email=$1", f"{subject}@corp.example") == 1


async def test_pending_refusal_commits_and_jti_is_consumed(pool, completion, monkeypatch):
    req, tokens, sign, _, subject = completion
    monkeypatch.setattr(settings, "keycloak_authoritative_email_domains_by_provider", {})
    access, identity = tokens()
    assertion = sign(access, identity)
    with pytest.raises(MembershipRequiredError):
        await service.complete_companion_login(req, access, identity, assertion)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM pending_admissions WHERE subject=$1", subject) == 1
    with pytest.raises(AuthenticationError, match="already used"):
        await service.complete_companion_login(req, access, identity, assertion)


async def test_assertion_consumed_after_account_failure(pool, completion, monkeypatch):
    req, tokens, sign, _, _ = completion
    access, identity = tokens()
    assertion = sign(access, identity)
    monkeypatch.setattr(service, "project_verified_principal_with_reason", AsyncMock(side_effect=RuntimeError("failure")))
    with pytest.raises(RuntimeError, match="failure"):
        await service.complete_companion_login(req, access, identity, assertion)
    with pytest.raises(AuthenticationError, match="already used"):
        await service.complete_companion_login(req, access, identity, assertion)


async def test_expired_at_db_consumption_refused(pool, completion):
    import time
    with pytest.raises(AuthenticationError):
        await service._consume_assertion("reef", str(uuid.uuid4()), int(time.time()) - 1)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM companion_login_assertions") == 0
