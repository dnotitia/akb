"""Mode-separated product-admin browser authentication contracts."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import settings
from app.exceptions import AuthenticationError


def _jwk(private_key: rsa.RSAPrivateKey) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()

    def encode(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "kid": "admin-test-key",
        "use": "sig",
        "alg": "RS256",
        "n": encode(numbers.n),
        "e": encode(numbers.e),
    }


@pytest.mark.asyncio
async def test_admin_authorization_request_uses_dedicated_client_pkce_and_nonce(
    monkeypatch,
) -> None:
    from app.services import keycloak_oidc

    issued: list[tuple[str, str, dict, int]] = []

    async def store(key: str, kind: str, payload: dict, ttl_secs: int) -> None:
        issued.append((key, kind, payload, ttl_secs))

    monkeypatch.setattr(keycloak_oidc, "_store_issue", store)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://id.example", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example", raising=False)

    request = await keycloak_oidc.KeycloakOIDC().begin_admin_login()
    query = parse_qs(urlsplit(request.location).query)

    assert query["client_id"] == ["akb-admin"]
    assert query["redirect_uri"] == ["https://akb.example/api/v1/admin/auth/keycloak/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid profile email"]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert "nonce" in query
    assert "prompt" not in query
    assert issued == [
        (
            query["state"][0],
            "admin-state-v1",
            {
                "code_verifier": issued[0][2]["code_verifier"],
                "nonce": query["nonce"][0],
                "browser_binding_hash": hashlib.sha256(request.browser_binding.encode("ascii")).hexdigest(),
            },
            600,
        )
    ]
    assert 43 <= len(issued[0][2]["code_verifier"]) <= 128
    assert request.browser_binding not in issued[0][2].values()


def test_admin_login_sets_browser_binding_cookie(monkeypatch) -> None:
    from app.api.routes import admin_auth
    from app.services.keycloak_oidc import AdminAuthorizationRequest

    class OIDC:
        async def begin_admin_login(self):
            return AdminAuthorizationRequest(
                location="https://id.example/authorize?state=one-time-state",
                browser_binding="browser-binding-value-that-is-long-enough",
            )

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example", raising=False)
    monkeypatch.setattr(admin_auth, "get_keycloak_oidc", lambda: OIDC())

    app = FastAPI()
    app.include_router(admin_auth.router, prefix="/api/v1")
    response = TestClient(app).get(
        "/api/v1/admin/auth/keycloak/login",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://id.example/authorize")
    binding_cookie = next(
        value for value in response.headers.get_list("set-cookie") if value.startswith("akb_admin_oidc_binding=")
    )
    assert "browser-binding-value-that-is-long-enough" in binding_cookie
    assert "HttpOnly" in binding_cookie
    assert "Path=/api/v1/admin/auth/keycloak/callback" in binding_cookie
    assert "SameSite=lax" in binding_cookie
    assert "Secure" in binding_cookie


@pytest.mark.asyncio
async def test_admin_code_exchange_uses_confidential_client_and_never_logs_tokens(
    monkeypatch,
    caplog,
) -> None:
    from app.services.keycloak_oidc import KeycloakOIDC

    requests: list[dict[str, str]] = []

    class Response:
        status_code = 200
        text = "must-not-be-logged-access-or-refresh-token"

        @staticmethod
        def json():
            return {"token_type": "Bearer", "id_token": "id-token"}

    class Client:
        async def post(self, _url: str, *, data: dict[str, str]):
            requests.append(data)
            return Response()

    monkeypatch.setattr(settings, "keycloak_server_url", "https://id.example", raising=False)
    monkeypatch.setattr(settings, "keycloak_internal_url", "", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
    monkeypatch.setattr(
        settings,
        "keycloak_admin_client_secret",
        "admin-client-secret",  # pragma: allowlist secret
        raising=False,
    )
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example", raising=False)
    service = KeycloakOIDC()
    monkeypatch.setattr(service, "_client", lambda: Client())

    result = await service.exchange_admin_code("one-time-code", "pkce-verifier")

    assert result == {"token_type": "Bearer", "id_token": "id-token"}
    assert requests == [
        {
            "grant_type": "authorization_code",
            "code": "one-time-code",
            "redirect_uri": "https://akb.example/api/v1/admin/auth/keycloak/callback",
            "client_id": "akb-admin",
            "client_secret": "admin-client-secret",  # pragma: allowlist secret
            "code_verifier": "pkce-verifier",
        }
    ]
    assert "must-not-be-logged" not in caplog.text
    assert "admin-client-secret" not in caplog.text


@pytest.mark.asyncio
async def test_admin_id_token_requires_exact_client_nonce_and_human_session(
    monkeypatch,
) -> None:
    from app.services.keycloak_oidc import KeycloakOIDC

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://id.example", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
    service = KeycloakOIDC()
    service._jwks = {"keys": [_jwk(private_key)]}
    now = int(datetime.now(timezone.utc).timestamp())
    expected_nonce = "expected-nonce-value-that-is-long-enough"

    def mint(**overrides: object) -> str:
        claims: dict[str, object] = {
            "iss": "https://id.example/realms/akb",
            "aud": "akb-admin",
            "azp": "akb-admin",
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "nonce": expected_nonce,
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            algorithm="RS256",
            headers={"kid": "admin-test-key", "typ": "JWT"},
        )

    claims = await service.verify_admin_id_token(
        mint(),
        expected_nonce=expected_nonce,
    )
    assert claims["aud"] == "akb-admin"

    for rejected in (
        mint(nonce="attacker-nonce"),
        mint(nonce="관리자-nonce"),
        mint(aud="akb-web"),
        mint(azp="akb-web"),
        mint(sid=""),
        mint(iat=str(now)),
        mint(exp=str(now + 300)),
    ):
        with pytest.raises(AuthenticationError):
            await service.verify_admin_id_token(
                rejected,
                expected_nonce=expected_nonce,
            )

    for malformed_key in (
        {
            "kty": "RSA",
            "kid": "admin-test-key",
            "use": "sig",
            "alg": "RS256",
            "n": "!!!",
            "e": "AQAB",
        },
        {
            "kty": "RSA",
            "kid": "admin-test-key",
            "use": "sig",
            "alg": "RS256",
            "e": "AQAB",
        },
    ):
        service._jwks = {"keys": [malformed_key]}
        with pytest.raises(AuthenticationError):
            await service.verify_admin_id_token(
                mint(),
                expected_nonce=expected_nonce,
            )


def test_admin_public_config_exposes_only_the_selected_login_surface(monkeypatch) -> None:
    from app.api.routes import admin_auth

    app = FastAPI()
    app.include_router(admin_auth.router, prefix="/api/v1")

    @app.exception_handler(AuthenticationError)
    async def authentication_error(_request, exc: AuthenticationError):
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)

    client = TestClient(app)

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    local = client.get("/api/v1/admin/auth/config")
    assert local.status_code == 200
    assert local.json() == {
        "schema_version": 1,
        "auth_mode": "local",
        "local": {"enabled": True, "login_url": "/api/v1/admin/auth/local/login"},
        "keycloak": {"enabled": False, "login_url": None},
    }

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_secret", "secret", raising=False)
    sso = client.get("/api/v1/admin/auth/config")
    assert sso.status_code == 200
    assert sso.json() == {
        "schema_version": 1,
        "auth_mode": "sso",
        "local": {"enabled": False, "login_url": None},
        "keycloak": {
            "enabled": True,
            "login_url": "/api/v1/admin/auth/keycloak/login",
        },
    }


def test_wrong_mode_admin_login_routes_are_hidden(monkeypatch) -> None:
    from app.api.routes import admin_auth

    app = FastAPI()
    app.include_router(admin_auth.router, prefix="/api/v1")
    client = TestClient(app)

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    assert (
        client.post(
            "/api/v1/admin/auth/local/login",
            json={"username": "admin", "password": "password"},  # pragma: allowlist secret
        ).status_code
        == 404
    )

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    assert client.get("/api/v1/admin/auth/keycloak/login").status_code == 404


def test_sso_admin_callback_issues_only_short_opaque_cookies(monkeypatch) -> None:
    from app.api.routes import admin_auth
    from app.services.admin_auth_service import (
        IssuedAdminBrowserSession,
        ProductAdminIdentity,
    )

    admin = ProductAdminIdentity(
        user_id=uuid.uuid4(),
        external_identity_id=uuid.uuid4(),
        username="admin",
        email="admin@example.com",
        display_name="Admin",
        auth_method="keycloak",
    )
    issued = IssuedAdminBrowserSession(
        token="opaque-session-value",
        csrf_token="opaque-csrf-value",
        expires_at=datetime.fromtimestamp(2_000_000_000, timezone.utc),
    )

    class OIDC:
        async def consume_admin_state(self, state: str, browser_binding: str):
            assert (state, browser_binding) == (
                "one-time-state",
                "browser-binding-value-that-is-long-enough",
            )
            return {"code_verifier": "verifier", "nonce": "nonce"}

        async def exchange_admin_code(self, code: str, verifier: str):
            assert (code, verifier) == ("one-time-code", "verifier")
            return {
                "token_type": "Bearer",
                "access_token": "keycloak-access-secret",
                "refresh_token": "keycloak-refresh-secret",
                "id_token": "keycloak-id-secret",
            }

        async def verify_admin_id_token(self, token: str, *, expected_nonce: str):
            assert (token, expected_nonce) == ("keycloak-id-secret", "nonce")
            return {
                "iss": "https://id.example/realms/akb",
                "sub": "admin-subject",
                "sid": "admin-sid",
                "exp": 2_000_000_000,
            }

    async def project(claims):
        assert claims["sub"] == "admin-subject"
        return admin

    async def create_session(identity, claims):
        assert identity == admin
        assert claims["sid"] == "admin-sid"
        return issued

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example", raising=False)
    monkeypatch.setattr(admin_auth, "get_keycloak_oidc", lambda: OIDC())
    monkeypatch.setattr(admin_auth, "resolve_prebound_sso_product_admin", project)
    monkeypatch.setattr(admin_auth, "create_sso_admin_browser_session", create_session)

    app = FastAPI()
    app.include_router(admin_auth.router, prefix="/api/v1")
    client = TestClient(app)
    client.cookies.set(
        "akb_admin_oidc_binding",
        "browser-binding-value-that-is-long-enough",
        path="/api/v1/admin/auth/keycloak/callback",
    )
    response = client.get(
        "/api/v1/admin/auth/keycloak/callback",
        params={"code": "one-time-code", "state": "one-time-state"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    cookies = response.headers.get_list("set-cookie")
    assert any(
        value.startswith("akb_admin_session=opaque-session-value;")
        and "HttpOnly" in value
        and "Path=/api/v1/admin" in value
        and "SameSite=lax" in value
        and "Secure" in value
        for value in cookies
    )
    assert any(
        value.startswith("akb_admin_oidc_binding=")
        and "Max-Age=0" in value
        and "Path=/api/v1/admin/auth/keycloak/callback" in value
        for value in cookies
    )
    assert any(
        value.startswith("akb_admin_csrf=opaque-csrf-value;")
        and "HttpOnly" not in value
        and "Path=/" in value
        and "SameSite=lax" in value
        and "Secure" in value
        for value in cookies
    )
    rendered = "\n".join(f"{name}: {value}" for name, value in response.headers.items())
    assert "keycloak-access-secret" not in rendered
    assert "keycloak-refresh-secret" not in rendered
    assert "keycloak-id-secret" not in rendered


def test_admin_session_is_mode_selected_and_sso_logout_requires_csrf(monkeypatch) -> None:
    from app.api.routes import admin_auth
    from app.services.admin_auth_service import ProductAdminIdentity

    identity = ProductAdminIdentity(
        user_id=uuid.uuid4(),
        external_identity_id=uuid.uuid4(),
        username="admin",
        email="admin@example.com",
        display_name=None,
        auth_method="keycloak",
    )
    calls: list[tuple[str, str, str]] = []

    async def resolve_sso(token: str):
        assert token == "opaque-session"
        return identity

    async def revoke(token: str, csrf_cookie: str, csrf_header: str):
        calls.append((token, csrf_cookie, csrf_header))
        return identity

    class OIDC:
        def admin_logout_url(self):
            return "https://id.example/logout?client_id=akb-admin"

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_secret", "secret", raising=False)
    monkeypatch.setattr(admin_auth, "resolve_sso_admin_browser_session", resolve_sso)
    monkeypatch.setattr(admin_auth, "revoke_sso_admin_browser_session", revoke)
    monkeypatch.setattr(admin_auth, "get_keycloak_oidc", lambda: OIDC())

    app = FastAPI()
    app.include_router(admin_auth.router, prefix="/api/v1")

    @app.exception_handler(AuthenticationError)
    async def authentication_error(_request, exc: AuthenticationError):
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)

    client = TestClient(app)
    client.cookies.set("akb_admin_session", "opaque-session")
    client.cookies.set("akb_admin_csrf", "csrf-value")

    session = client.get("/api/v1/admin/auth/session")
    assert session.status_code == 200
    assert session.json()["user"] == {
        "id": str(identity.user_id),
        "username": "admin",
        "email": "admin@example.com",
        "display_name": None,
        "is_admin": True,
    }

    assert client.post("/api/v1/admin/auth/logout").status_code == 401
    logout = client.post(
        "/api/v1/admin/auth/logout",
        headers={"X-AKB-Admin-CSRF": "csrf-value"},
    )
    assert logout.status_code == 200
    assert logout.json()["logout_url"].startswith("https://id.example/logout")
    assert calls == [("opaque-session", "csrf-value", "csrf-value")]


@pytest.mark.asyncio
async def test_admin_session_rejects_unbounded_keycloak_claims_before_database(
    monkeypatch,
) -> None:
    from app.services import admin_auth_service
    from app.services.admin_auth_service import ProductAdminIdentity

    identity = ProductAdminIdentity(
        user_id=uuid.uuid4(),
        external_identity_id=uuid.uuid4(),
        username="admin",
        email="admin@example.com",
        display_name=None,
        auth_method="keycloak",
    )

    async def no_database():
        raise AssertionError("invalid claims must fail before database access")

    monkeypatch.setattr(admin_auth_service, "get_pool", no_database)
    now = int(datetime.now(timezone.utc).timestamp())
    for claims in (
        {"sid": "s" * 256, "exp": now + 300},
        {"sid": "valid-sid", "exp": 10**100},
    ):
        with pytest.raises(AuthenticationError):
            await admin_auth_service.create_sso_admin_browser_session(
                identity,
                claims,
            )


@pytest.mark.asyncio
async def test_admin_session_rejects_non_urlsafe_csrf_before_database(
    monkeypatch,
) -> None:
    from app.services import admin_auth_service

    async def no_database():
        raise AssertionError("invalid credentials must fail before database access")

    monkeypatch.setattr(admin_auth_service, "get_pool", no_database)
    with pytest.raises(AuthenticationError):
        await admin_auth_service.revoke_sso_admin_browser_session(
            "s" * 32,
            "관리자-csrf-token-value-that-is-long-enough",
            "관리자-csrf-token-value-that-is-long-enough",
        )
