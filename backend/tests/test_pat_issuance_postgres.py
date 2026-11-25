"""Real PostgreSQL contracts for strict PAT issuance and issuer authority."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.deps import get_current_user
from app.api.routes import access, auth
from app.db import postgres
from app.exceptions import AKBError
from app.models.vault_scope import current_key_class, current_token_id, current_token_scopes, current_vault_scope
from app.services import auth_service
from app.services.auth_service import AuthenticatedUser
from tests.test_recovery_admin_provisioning_postgres import _fresh_database

pytestmark = [pytest.mark.asyncio, pytest.mark.live_services]


@pytest.fixture(autouse=True)
def import_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_service.settings, "git_storage_path", str(tmp_path / "vaults"))


def body(actor, **changes):
    return {"contract_version": 1, "expected_user_id": actor.user_id, "name": " laptop ",
            "scopes": ["read", "write"], "vault_scope": None, **changes}


async def person(pool, *, admin=False):
    uid = uuid.uuid4()
    await pool.execute("""INSERT INTO users(id,username,email,password_hash,is_admin)
        VALUES($1,$2,$3,'unusable',$4)""", uid, uid.hex, f"{uid}@example.test", admin)
    return AuthenticatedUser(str(uid), uid.hex, f"{uid}@example.test", None, admin, "jwt")


def wire(monkeypatch, pool):
    get_pool = AsyncMock(return_value=pool)
    monkeypatch.setattr(postgres, "get_pool", get_pool)
    monkeypatch.setattr(auth_service, "get_pool", get_pool)
    monkeypatch.setattr(auth_service, "get_role_sync", lambda: AsyncMock())
    monkeypatch.setattr(auth_service.settings, "admin_token_issuer_ids", [])


@asynccontextmanager
async def client_for(actor):
    from app.main import akb_error_handler, validation_error_handler
    from app.http_middleware import NoStorePublicSurfacesMiddleware
    app = FastAPI()
    app.add_middleware(NoStorePublicSurfacesMiddleware)
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(access.router, prefix="/api/v1")
    app.add_exception_handler(AKBError, akb_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    if actor is not None:
        app.dependency_overrides[get_current_user] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        yield client


async def test_receipt_scope_list_expiry_and_legacy_defaults(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        actor = await person(pool)
        async with client_for(actor) as client:
            caps = await client.get("/api/v1/auth/tokens/capabilities")
            assert caps.status_code == 200 and caps.headers["cache-control"] == "no-store"
            assert caps.json()["user_id"] == actor.user_id
            result = await client.post("/api/v1/auth/tokens/issuance", json=body(actor, scopes=["read"],
                vault_scope={"prefixes": [" team-", "team-"], "extra_vaults": ["z", "a"]}, expires_days=30))
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "no-store"
            receipt = result.json()
            assert receipt["contract_version"] == 1 and receipt["name"] == "laptop"
            assert receipt["scopes"] == ["read"] and receipt["key_class"] == "pat"
            assert receipt["vault_scope"] == {"prefixes": ["team-"], "extra_vaults": ["a", "z"]}
            assert datetime.fromisoformat(receipt["expires_at"]) - datetime.fromisoformat(receipt["issued_at"]) == timedelta(days=30)
            stored = await pool.fetchrow("SELECT * FROM tokens WHERE id=$1", uuid.UUID(receipt["token_id"]))
            assert stored["expires_at"] == datetime.fromisoformat(receipt["expires_at"])
            assert stored["token_hash"] == auth_service._hash_token(receipt["token"])
            listing = await client.get("/api/v1/auth/tokens")
            assert listing.headers["cache-control"] == "no-store"
            listed = listing.json()["tokens"][0]
            assert listed["vault_scope"] == receipt["vault_scope"] and "token" not in listed
            event = await pool.fetchval("SELECT payload FROM events WHERE kind='auth.token_issued'")
            assert receipt["token"] not in event and json.loads(event)["token_id"] == receipt["token_id"]
            for payload in ({"name": "old"}, {"name": "old", "expires_days": 0}, {"name": "old", "expires_days": None}):
                legacy = await client.post("/api/v1/auth/tokens", json=payload)
                assert legacy.status_code == 200 and legacy.json()["expires_at"] is None
                assert legacy.headers["cache-control"] == "no-store"
                assert legacy.json()["scopes"] == ["read", "write"] and legacy.json()["vault_scope"] is None
            instant = datetime.now(timezone.utc) + timedelta(hours=2, microseconds=234567)
            absolute = await client.post("/api/v1/auth/tokens/issuance", json=body(actor, expires_at=instant.isoformat()))
            assert absolute.status_code == 200 and datetime.fromisoformat(absolute.json()["expires_at"]) == instant


@pytest.mark.parametrize("changes,field", [({"expires_at": "2000000000"}, "expires_at"),
    pytest.param({"expires_at": "9999-12-31T23:59:59-01:00"}, "expires_at", id="utc-max-overflow"),
    pytest.param({"expires_at": "0001-01-01T00:00:00+01:00"}, "expires_at", id="utc-min-overflow"),
    ({"expires_days": 10**30}, "expires_days"), ({"scopes": ["admin"]}, "scopes"),
    ({"vault_scope": {"prefixes": ["bad*"], "extra_vaults": []}}, "vault_scope.prefixes.0"),
    ({"unexpected": "never-echo-this"}, "unexpected")])
async def test_422_fields_and_no_insert(monkeypatch, auth_schema_template, changes, field):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        actor = await person(pool)
        async with client_for(actor) as client:
            result = await client.post("/api/v1/auth/tokens/issuance", json=body(actor, **changes))
        assert result.status_code == 422, result.text
        assert result.json()["code"] == "token_issuance_validation"
        assert result.json()["details"]["fields"][0]["field"] == field
        assert "never-echo-this" not in result.text
        assert await pool.fetchval("SELECT count(*) FROM tokens") == 0


async def test_identity_and_authoritative_human_guards(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        actor = await person(pool)
        async with client_for(actor) as client:
            response = await client.post("/api/v1/auth/tokens/issuance", json=body(actor, expected_user_id=str(uuid.uuid4())))
            assert response.status_code == 409 and response.json()["code"] == "token_issuer_identity_changed"
            await pool.execute("UPDATE users SET account_status='suspended' WHERE id=$1", uuid.UUID(actor.user_id))
            for route in ("/auth/tokens", "/auth/tokens/issuance"):
                response = await client.post("/api/v1" + route, json={"name": "old"} if route.endswith("tokens") else body(actor))
                assert response.status_code == 403
        assert await pool.fetchval("SELECT count(*) FROM tokens") == 0


@pytest.mark.parametrize("key_class", ["pat", "service"])
async def test_token_carriers_cannot_mint_through_any_alias(monkeypatch, auth_schema_template, key_class):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        human = await person(pool, admin=True)
        credential = await auth_service.create_pat(human.user_id, "issuer", key_class=key_class)
        actor = await auth_service._resolve_pat(credential["token"])
        async with client_for(actor) as client:
            routes = [("/auth/tokens", {"name": "child"}), ("/auth/tokens/issuance", body(actor)),
                      (f"/admin/users/{actor.user_id}/tokens", {"name": "child"}),
                      (f"/admin/users/{actor.user_id}/managed-tokens", {"name": "child", "token_id": str(uuid.uuid4())})]
            for path, payload in routes:
                response = await client.post("/api/v1" + path, json=payload)
                assert response.status_code == 403, (path, response.text)
            assert (await client.get("/api/v1/auth/tokens/capabilities")).status_code == 403
        assert await pool.fetchval("SELECT count(*) FROM tokens") == 1


async def test_allowlisted_machine_admin_aliases_and_live_revalidation(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        human, target = await person(pool, admin=True), await person(pool)
        credential = await auth_service.create_pat(human.user_id, "approved", key_class="service")
        actor = await auth_service._resolve_pat(credential["token"])
        tid = uuid.UUID(actor.token_id)
        monkeypatch.setattr(auth_service.settings, "admin_token_issuer_ids", [tid])
        async with client_for(actor) as client:
            for alias in ("tokens", "managed-tokens"):
                response = await client.post(f"/api/v1/admin/users/{target.user_id}/{alias}", json={"name": "managed", "token_id": str(uuid.uuid4())})
                assert response.status_code == 200, response.text
            assert (await client.post("/api/v1/auth/tokens", json={"name": "self"})).status_code == 403
            for sql in ("UPDATE tokens SET scopes=ARRAY['read'] WHERE id=$1",
                        "UPDATE tokens SET scopes=ARRAY['read','write'], vault_scope='{}' WHERE id=$1",
                        "UPDATE tokens SET vault_scope=NULL, expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",
                        "DELETE FROM tokens WHERE id=$1"):
                await pool.execute(sql, tid)
                response = await client.post(f"/api/v1/admin/users/{target.user_id}/tokens", json={"name": "blocked"})
                assert response.status_code == 403 and response.json()["code"] == "machine_token_issuer_invalid"
        assert await pool.fetchval("SELECT count(*) FROM tokens WHERE user_id=$1", uuid.UUID(target.user_id)) == 2


async def test_expiration_rechecked_after_waiting_for_user_lock(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        actor = await person(pool)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT id FROM users WHERE id=$1 FOR UPDATE", uuid.UUID(actor.user_id))
                expiry = datetime.now(timezone.utc) + timedelta(milliseconds=120)
                pending = asyncio.create_task(auth_service.create_pat(actor.user_id, "expired waiting", issuer=actor, expires_at=expiry))
                await asyncio.sleep(0.2)
            with pytest.raises(AKBError) as exc:
                await pending
        assert exc.value.code == "token_issuance_validation"
        assert await pool.fetchval("SELECT count(*) FROM tokens") == 0


async def test_audit_failure_rolls_back_mint(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        actor = await person(pool)
        monkeypatch.setattr(auth_service, "emit_event", AsyncMock(side_effect=RuntimeError("audit unavailable")))
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await auth_service.create_pat(actor.user_id, "atomic", issuer=actor)
        assert await pool.fetchval("SELECT count(*) FROM tokens") == 0


async def test_trusted_absolute_utc_overflow_never_inserts(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        actor = await person(pool)
        for instant in ("9999-12-31T23:59:59-01:00", "0001-01-01T00:00:00+01:00"):
            with pytest.raises(AKBError) as exc:
                await auth_service.create_pat(actor.user_id, "overflow", expires_at=datetime.fromisoformat(instant))
            assert exc.value.status_code == 422
            assert exc.value.details["fields"][0]["field"] == "expires_at"
            assert exc.value.details["fields"][0]["code"] == "expiration_overflow"
        assert await pool.fetchval("SELECT count(*) FROM tokens") == 0


async def test_sso_human_issuance_and_browser_csrf(monkeypatch, browser_auth_template):
    from app.api import deps
    from app.services import sso_browser_session_service as sessions
    from tests.test_sso_account_lifecycle_postgres import configure, issue
    from tests.test_sso_browser_session_postgres import _seed_identity, _actor, _fresh_database as sso_database
    async with sso_database(browser_auth_template) as pool:
        wire(monkeypatch, pool)
        configure(monkeypatch, pool)
        monkeypatch.setattr(deps, "sso_browser_session_ready", lambda: True)
        uid, _ = await _seed_identity(pool)
        await pool.execute("UPDATE users SET credential_change_required=true WHERE id=$1", uid)
        actor = replace(_actor(uid), auth_method="oauth")
        async with client_for(actor) as client:
            result = await client.post("/api/v1/auth/tokens/issuance", json=body(actor))
            assert result.status_code == 200, result.text
        handle = await issue(uid, await sessions.next_browser_login_sequence())
        async with client_for(None) as client:
            client.cookies.set(sessions.sso_browser_session_cookie_name(), handle.token)
            client.cookies.set(sessions.sso_browser_csrf_cookie_name(), handle.csrf_token)
            denied = await client.post("/api/v1/auth/tokens/issuance", json=body(actor))
            assert denied.status_code == 403
            allowed = await client.post("/api/v1/auth/tokens/issuance", json=body(actor),
                                        headers={sessions.SSO_BROWSER_CSRF_HEADER: handle.csrf_token})
            assert allowed.status_code == 200, allowed.text
            pat = allowed.json()
            monkeypatch.setattr(auth_service.settings, "pat_enabled", False)

            listed = await client.get("/api/v1/auth/tokens")
            assert listed.status_code == 200
            assert any(token["token_id"] == pat["token_id"] for token in listed.json()["tokens"])
            bearer = {"Authorization": f"Bearer {pat['token']}"}
            denied = await client.get("/api/v1/auth/tokens", headers=bearer)
            assert denied.status_code == 401
            assert await pool.fetchval(
                "SELECT last_used_at FROM tokens WHERE id = $1", uuid.UUID(pat["token_id"]),
            ) is None


@pytest.mark.parametrize("mutation", ["is_admin=false", "account_status='suspended'"])
async def test_admin_owner_changes_deny_captured_authorization(monkeypatch, auth_schema_template, mutation):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        admin, target = await person(pool, admin=True), await person(pool)
        credential = await auth_service.create_pat(admin.user_id, "approved", key_class="service")
        machine = await auth_service._resolve_pat(credential["token"])
        monkeypatch.setattr(auth_service.settings, "admin_token_issuer_ids", [uuid.UUID(machine.token_id)])
        await pool.execute(f"UPDATE users SET {mutation} WHERE id=$1", uuid.UUID(admin.user_id))
        for actor in (admin, machine):
            async with client_for(actor) as client:
                denied = await client.post(f"/api/v1/admin/users/{target.user_id}/tokens", json={"name": "denied"})
                assert denied.status_code == 403, denied.text
        assert await pool.fetchval("SELECT count(*) FROM tokens WHERE user_id=$1", uuid.UUID(target.user_id)) == 0


async def test_human_admin_both_aliases_and_suspended_target(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        admin, target = await person(pool, admin=True), await person(pool)
        async with client_for(admin) as client:
            for alias in ("tokens", "managed-tokens"):
                issued = await client.post(f"/api/v1/admin/users/{target.user_id}/{alias}", json={"name": "human issued", "token_id": str(uuid.uuid4())})
                assert issued.status_code == 200, issued.text
            await pool.execute("UPDATE users SET account_status='suspended' WHERE id=$1", uuid.UUID(target.user_id))
            denied = await client.post(f"/api/v1/admin/users/{target.user_id}/tokens", json={"name": "denied"})
            assert denied.status_code == 403, denied.text
        assert await pool.fetchval("SELECT count(*) FROM tokens WHERE user_id=$1", uuid.UUID(target.user_id)) == 2


async def test_special_scope_cannot_be_minted_by_admin_or_allowlisted_machine(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        admin, target = await person(pool, admin=True), await person(pool)
        await pool.execute("UPDATE users SET account_kind='service' WHERE id=$1", uuid.UUID(target.user_id))
        credential = await auth_service.create_pat(admin.user_id, "approved", key_class="service")
        machine = await auth_service._resolve_pat(credential["token"])
        monkeypatch.setattr(auth_service.settings, "admin_token_issuer_ids", [uuid.UUID(machine.token_id)])
        for actor in (admin, machine):
            async with client_for(actor) as client:
                for alias in ("tokens", "managed-tokens"):
                    for scopes in (["sso_operation_requests"], ["sso_operation_requests", "admin"]):
                        result = await client.post(f"/api/v1/admin/users/{target.user_id}/{alias}",
                            json={"name": "denied", "token_id": str(uuid.uuid4()),
                                  "key_class": "service", "scopes": scopes})
                        assert (result.status_code, result.json()["code"]) == (
                            403, "sso_operation_request_scope_issuance_required"
                        )
        with pytest.raises(AKBError) as no_issuer:
            await auth_service.create_pat(target.user_id, "denied", key_class="service",
                                          scopes=["sso_operation_requests"], admin_issuance=True)
        assert no_issuer.value.code == "sso_operation_request_scope_issuance_required"
        assert await pool.fetchval("SELECT COUNT(*) FROM tokens WHERE user_id=$1", uuid.UUID(target.user_id)) == 0


async def test_disabled_pat_issuance_aliases_and_management(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        admin = await person(pool, admin=True)
        existing = await auth_service.create_pat(admin.user_id, "existing")
        monkeypatch.setattr(auth_service.settings, "pat_enabled", False)

        async with client_for(admin) as client:
            alias = f"/api/v1/admin/users/{admin.user_id}"
            requests = [
                ("GET", "/api/v1/auth/tokens/capabilities", None),
                ("POST", "/api/v1/auth/tokens", {"name": "legacy"}),
                ("POST", "/api/v1/auth/tokens/issuance", body(admin)),
                ("POST", f"{alias}/tokens", {"name": "admin"}),
                ("POST", f"{alias}/managed-tokens", {"name": "managed", "token_id": str(uuid.uuid4())}),
            ]
            for method, path, payload in requests:
                response = await client.request(method, path, json=payload)
                assert (response.status_code, response.json()["code"]) == (
                    403, "personal_access_tokens_disabled",
                )

            assert await pool.fetchval("SELECT count(*) FROM tokens") == 1
            service = await client.post(f"{alias}/tokens", json={"name": "machine", "key_class": "service"})
            assert service.status_code == 200 and service.json()["key_class"] == "service"
            listed = await client.get("/api/v1/auth/tokens")
            assert any(token["token_id"] == existing["token_id"] for token in listed.json()["tokens"])
            assert (await client.delete(f"/api/v1/auth/tokens/{existing['token_id']}")).status_code == 200

        monkeypatch.setattr(auth_service.settings, "pat_enabled", True)
        assert await auth_service.resolve_rest_user_authorization(f"Bearer {existing['token']}") is None


async def test_disabled_pat_preserves_atomic_verification_and_stored_class(monkeypatch, auth_schema_template):
    async with _fresh_database(auth_schema_template) as pool:
        wire(monkeypatch, pool)
        admin = await person(pool, admin=True)
        saved = await auth_service.create_pat(admin.user_id, "saved")
        legacy = await auth_service.create_pat(admin.user_id, "null class")
        promoted = await auth_service.create_pat(admin.user_id, "promoted", scopes=["read"])
        expired = await auth_service.create_pat(admin.user_id, "expired")

        await pool.execute("ALTER TABLE tokens ALTER COLUMN key_class DROP NOT NULL")
        await pool.execute("UPDATE tokens SET key_class = NULL WHERE id = $1", uuid.UUID(legacy["token_id"]))
        await pool.execute("UPDATE tokens SET key_class = 'service' WHERE id = $1", uuid.UUID(promoted["token_id"]))
        await pool.execute("UPDATE tokens SET expires_at = NOW() WHERE id = $1", uuid.UUID(expired["token_id"]))
        monkeypatch.setattr(auth_service.settings, "pat_enabled", False)

        for resolve in (
            auth_service.resolve_rest_user_authorization,
            auth_service.resolve_mcp_authorization,
            auth_service.resolve_tool_control_authorization,
        ):
            assert (await resolve(f"Bearer {promoted['token']}")).key_class == "service"
            for credential in (saved, legacy, expired):
                assert await resolve(f"Bearer {credential['token']}") is None
                assert current_key_class.get() is current_token_id.get() is current_token_scopes.get() is current_vault_scope.get() is None

        assert await pool.fetchval("SELECT count(*) FROM tokens WHERE last_used_at IS NOT NULL") == 1
        monkeypatch.setattr(auth_service.settings, "pat_enabled", True)
        assert (await auth_service.resolve_mcp_authorization(f"Bearer {saved['token']}")).key_class == "pat"
        assert (await auth_service.resolve_mcp_authorization(f"Bearer {legacy['token']}")).key_class == "pat"
        assert await auth_service.resolve_mcp_authorization(f"Bearer {expired['token']}") is None
