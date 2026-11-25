from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from app.api.routes import auth
from app.config import settings
from app.exceptions import AuthenticationError, ForbiddenError
from app.services import account_service, admin_auth_service, auth_service, monitoring_auth
from app.services.auth_verifier_profiles import KEYCLOAK_ACCESS_V1, VerifiedPrincipal
from app.services.sso_browser_session_service import revoke_sso_browser_sessions_from_logout_token
from app.services.sso_session_epoch import reconcile_sso_session_epoch
from tests.test_event_tail_postgres import _fresh_database

pytestmark = pytest.mark.live_services


REPORT = "/api/v1/admin/mcp-monitoring"


async def test_sso_monitoring_callback_issues_only_monitoring_authority(monkeypatch, tmp_path, application_schema_template):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import app

    monkeypatch.setattr(settings, "auth_mode", "sso")
    monkeypatch.setattr(settings, "keycloak_enabled", True)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://id.example")
    monkeypatch.setattr(settings, "keycloak_realm", "test")
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web")
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only")
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example")
    monkeypatch.setattr(settings, "sso_session_epoch", uuid4())
    monkeypatch.setattr(auth, "sso_browser_session_ready", lambda: True)
    monitor, admin, member = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    claims = {
        "iss": settings.keycloak_issuer, "sub": "monitor", "sid": "monitor-sid",
        "scope": "openid", "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }

    async with _fresh_database(application_schema_template) as pool:
        await reconcile_sso_session_epoch()
        await pool.executemany(
            """
            INSERT INTO users(id, username, email, password_hash, auth_provider, is_admin)
            VALUES ($1, $2, $3, 'disabled', 'keycloak', $4)
            """,
            [(monitor, "monitor", "monitor@example.org", False), (admin, "admin", "admin@example.org", True),
             (member, "member", "member@example.org", False)],
        )
        await pool.executemany(
            "INSERT INTO external_identities (id, user_id, issuer, subject) VALUES ($1,$2,$3,$4)",
            [(uuid4(), monitor, settings.keycloak_issuer, "monitor"), (uuid4(), admin, settings.keycloak_issuer, "admin")],
        )
        await monitoring_auth.set_monitoring_access(monitor, True, actor_id=str(admin))
        with pytest.raises(ForbiddenError):
            await account_service.set_user_admin(str(monitor), is_admin=True, actor_id=str(admin))
        with pytest.raises(ForbiddenError):
            await monitoring_auth.set_monitoring_access(admin, True, actor_id=str(admin))

        principal = VerifiedPrincipal(
            KEYCLOAK_ACCESS_V1, settings.keycloak_issuer, "monitor", "access_token", claims, None,
        )
        oidc = type("OidcFixture", (), {})()
        oidc.consume_browser_state = AsyncMock(return_value={
            "redirect_path": "/admin/mcp-monitoring", "purpose": "monitoring", "client_id": "akb-web",
            "provider_alias": "local", "code_verifier": "verified", "nonce": "fixture",
        })
        oidc.exchange_browser_code = AsyncMock(return_value={
            "token_type": "Bearer", "access_token": "fixture-access", "id_token": "fixture-id",
        })
        oidc.verify_access_token = AsyncMock(return_value=principal)
        oidc.verify_browser_id_token = AsyncMock(return_value=claims)
        monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: oidc)
        assert await auth_service.project_verified_principal(principal) is None

        admin_claims = {**claims, "sub": "admin", "sid": "admin-sid"}
        identity = await admin_auth_service.resolve_prebound_sso_product_admin(admin_claims)
        admin_session = await admin_auth_service.create_sso_admin_browser_session(identity, admin_claims)
        personal = await auth_service.create_pat(str(admin), "personal")
        machine = await auth_service.create_pat(str(admin), "issuer", key_class="service", scopes=["read", "write"])
        monkeypatch.setattr(settings, "admin_token_issuer_ids", [UUID(machine["token_id"])])

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://akb.example",
        ) as client:
            callback = await client.get("/api/v1/auth/keycloak/callback", params={"state": "fixture", "code": "fixture"})
            assert callback.status_code == 303 and callback.headers["Location"] == "/admin/mcp-monitoring"
            assert "__Host-akb_sso_session" not in client.cookies and "__Host-akb_admin_session" not in client.cookies
            token = client.cookies["__Secure-akb_monitor_session"]
            bearer = {"Authorization": "Bearer " + token}
            start = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            end = (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            reads = [
                (REPORT, 200),
                (f"{REPORT}/operations/{member}/{uuid4()}/calls?from={start}&to={end}", 200),
                (f"{REPORT}/calls/{uuid4()}/trace", 404),
            ]
            for path, expected in reads:
                assert (await client.get(path, headers=bearer)).status_code == expected
                assert (await client.get(path)).status_code == expected
                assert (await client.get(path, headers={"Authorization": ""})).status_code == 401

            protected = [
                ("GET", "/api/v1/admin/auth/session", None),
                ("GET", "/api/v1/my/vaults", None),
                ("POST", f"/api/v1/admin/users/{member}/tokens", {"name": "fixture"}),
                ("PUT", f"/api/v1/admin/users/{monitor}/role", {"is_admin": True}),
                ("POST", f"/api/v1/admin/sso/operation-requests/{uuid4()}/approve", {"expected_request_sha256": "0" * 64}),
                ("POST", "/api/v1/admin/sso/providers/fixture/disable", None),
                ("POST", "/api/v1/tables/fixture/sql", {"sql": "SELECT 1"}),
            ]
            monitor_cookie = {"Cookie": f"__Secure-akb_monitor_session={token}"}
            csrf = {"X-AKB-Admin-CSRF": admin_session.csrf_token, "X-AKB-CSRF": "fixture"}
            for method, path, body in protected:
                for headers in (bearer, monitor_cookie, {**monitor_cookie, **csrf}):
                    response = await client.request(method, path, headers=headers, json=body)
                    assert response.status_code in (401, 403), (method, path, response.status_code)
            assert await pool.fetchval("SELECT count(*) FROM tokens WHERE user_id=$1", member) == 0
            assert not await pool.fetchval("SELECT is_admin FROM users WHERE id=$1", monitor)

            admin_cookie = {"Cookie": f"__Host-akb_admin_session={admin_session.token}"}
            assert (await client.get("/api/v1/admin/auth/session", headers=admin_cookie)).status_code == 200
            assert (await client.get("/api/v1/admin/auth/session", headers={**admin_cookie, **bearer})).status_code == 401
            for credential, expected in ((personal, 403), (machine, 200)):
                headers = {"Authorization": "Bearer " + credential["token"]}
                assert (await client.get(REPORT, headers=headers)).status_code == 401
                response = await client.post(f"/api/v1/admin/users/{member}/tokens", headers=headers, json={"name": "fixture"})
                assert response.status_code == expected

            with monkeypatch.context() as policy:
                policy.setattr(settings, "keycloak_enrollment_mode", "disabled")
                assert (await client.get(REPORT)).status_code == 401
                refused = await client.get("/api/v1/auth/keycloak/callback", params={"state": "fixture", "code": "fixture"})
                assert refused.headers["Location"].endswith("?auth_error=sign_in_failed")

            await revoke_sso_browser_sessions_from_logout_token(
                issuer=claims["iss"], sid=claims["sid"], subject=claims["sub"],
                issued_at=claims["iat"] + 1, expires_at=claims["exp"],
            )
            assert (await client.get(REPORT)).status_code == 401
            with pytest.raises(AuthenticationError):
                await monitoring_auth.login_monitoring_sso(monitoring_auth.MonitoringSsoProof.from_verified_tokens(claims, claims))
