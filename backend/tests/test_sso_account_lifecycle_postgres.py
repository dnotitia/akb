"""Real SSO handle revocation and callback ordering without touching IdP/PATs."""
from dataclasses import replace
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.exceptions import AKBError, AuthenticationError
from app.services import account_self_service as lifecycle
from app.services import sso_browser_session_service as sessions
from tests.test_sso_browser_session_postgres import (
    _fresh_database, _configure, _seed_identity, _actor, _principal, _tokens,
)

pytestmark = pytest.mark.asyncio


async def issue(uid, sequence=None):
    principal = _principal()
    return await sessions.create_sso_browser_session(
        _actor(uid), principal,
        {"iss": principal.issuer, "sub": principal.subject, "sid": principal.claims["sid"],
         "identity_provider": "workforce"}, _tokens(), login_sequence=sequence,
    )


def configure(monkeypatch, pool):
    _configure(monkeypatch, sessions, pool)
    monkeypatch.setattr(lifecycle, "get_pool", sessions.get_pool)
    monkeypatch.setattr(lifecycle.settings, "account_self_service_enabled", True)


async def test_sso_all_logout_preserves_pat_account_vault_and_fences_callbacks(monkeypatch):
    async with _fresh_database() as pool:
        configure(monkeypatch, pool)
        uid, _ = await _seed_identity(pool)
        actor = replace(_actor(uid), auth_method="browser_session")
        before = await sessions.next_browser_login_sequence()
        first, second = await issue(uid, before), await issue(uid, before)
        tid, vid = uuid.uuid4(), uuid.uuid4()
        await pool.execute("""INSERT INTO tokens(id,user_id,name,token_hash,token_prefix)
            VALUES($1,$2,'fixture','fixture-hash','fixture')""", tid, uid)
        await pool.execute("INSERT INTO vaults(id,name,owner_id,git_path) VALUES($1,'preserved',$2,'/unused')", vid, uid)
        preview = await lifecycle.lifecycle(actor)
        assert preview["revoke_sessions"] == {"supported": True, "scope": "sso_browser_sessions",
            "includes_current": True, "affects_pats": False, "reason": None}
        assert not preview["deletion"]["supported"] and preview["deletion"]["reason"] == "managed_account"
        result = await lifecycle.revoke_sessions(actor, uid)
        assert result["user_id"] == str(uid)
        for handle in (first, second):
            with pytest.raises(AuthenticationError):
                await sessions.resolve_sso_browser_session(handle.token)
        for table, resource in (("users", uid), ("tokens", tid), ("vaults", vid)):
            assert await pool.fetchval(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE id=$1)", resource)
        for stale in (None, before):
            with pytest.raises(AuthenticationError, match="started before"):
                await issue(uid, stale)
        fresh = await issue(uid, await sessions.next_browser_login_sequence())
        assert await sessions.resolve_sso_browser_session(fresh.token) is not None
        with pytest.raises(AKBError) as exc:
            await lifecycle.delete_account(actor, uid, actor.username, "unused")
        assert exc.value.code == "managed_account"


async def test_sso_http_requires_csrf_identity_and_does_not_clear_newer_cookies(monkeypatch):
    from app.api import deps
    from app.api.routes.access import router
    async with _fresh_database() as pool:
        configure(monkeypatch, pool)
        monkeypatch.setattr(deps, "sso_browser_session_ready", lambda: True)
        uid, _ = await _seed_identity(pool)
        handle = await issue(uid, await sessions.next_browser_login_sequence())
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        @app.exception_handler(AKBError)
        async def errors(request, exc):
            return JSONResponse({"code": exc.code}, status_code=exc.status_code)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
            client.cookies.set(sessions.sso_browser_session_cookie_name(), handle.token)
            client.cookies.set(sessions.sso_browser_csrf_cookie_name(), handle.csrf_token)
            url = "/api/v1/my/account/session-revocations"
            assert (await client.post(url, json={"expected_user_id": str(uid)})).status_code == 403
            headers = {sessions.SSO_BROWSER_CSRF_HEADER: handle.csrf_token}
            assert (await client.post(url, headers=headers, json={"expected_user_id": str(uuid.uuid4())})).status_code == 409
            response = await client.post(url, headers=headers, json={"expected_user_id": str(uid)})
            assert response.status_code == 200 and "set-cookie" not in response.headers
            assert (await client.get("/api/v1/my/account/lifecycle")).status_code == 401


async def test_sso_logout_audit_failure_rolls_back_handles_and_fence(monkeypatch):
    from unittest.mock import AsyncMock
    async with _fresh_database() as pool:
        configure(monkeypatch, pool)
        uid, _ = await _seed_identity(pool)
        handle = await issue(uid, await sessions.next_browser_login_sequence())
        monkeypatch.setattr(sessions, "emit_event", AsyncMock(side_effect=RuntimeError("fixture audit outage")))
        with pytest.raises(RuntimeError, match="fixture audit outage"):
            await lifecycle.revoke_sessions(replace(_actor(uid), auth_method="browser_session"), uid)
        assert await pool.fetchval("SELECT count(*) FROM sso_browser_user_revocations") == 0
        assert await sessions.resolve_sso_browser_session(handle.token) is not None


async def test_concurrent_callback_cannot_survive_account_wide_logout(monkeypatch):
    import asyncio
    async with _fresh_database() as pool:
        configure(monkeypatch, pool)
        uid, _ = await _seed_identity(pool)
        before = await sessions.next_browser_login_sequence()
        actor = replace(_actor(uid), auth_method="browser_session")
        results = await asyncio.gather(issue(uid, before), lifecycle.revoke_sessions(actor, uid), return_exceptions=True)
        assert isinstance(results[1], dict)
        assert isinstance(results[0], (sessions.IssuedSsoBrowserSession, AuthenticationError))
        assert await pool.fetchval("SELECT count(*) FROM sso_browser_sessions WHERE user_id=$1", uid) == 0
