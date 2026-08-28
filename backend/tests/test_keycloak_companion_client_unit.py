"""Ordinary-browser OIDC profile uses one server-owned AKB client.

Companion applications authenticate with their own BFF/client and present a
Keycloak access token to AKB. They never select the client or callback used by
AKB's own browser session.
"""

from __future__ import annotations

import hashlib
import urllib.parse

import pytest

from app.config import settings
from app.exceptions import AuthenticationError
from app.services import keycloak_oidc
from app.services.keycloak_oidc import KeycloakOIDC


@pytest.mark.asyncio
async def test_begin_browser_login_is_nonce_pkce_and_browser_bound(monkeypatch):
    issued: dict[str, object] = {}

    async def capture_issue(key, kind, payload, ttl_secs):
        issued.update(key=key, kind=kind, payload=payload, ttl_secs=ttl_secs)

    monkeypatch.setattr(keycloak_oidc, "_store_issue", capture_issue)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web")
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com")

    request = await KeycloakOIDC().begin_browser_login(
        "/vaults?selected=one",
        provider_alias="workforce",
    )

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.location).query)
    assert query["client_id"] == ["akb-web"]
    assert query["redirect_uri"] == ["https://akb.example.com/api/v1/auth/keycloak/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid profile email"]
    assert query["kc_idp_hint"] == ["workforce"]
    # Provider selection must win over any native/broker session already in
    # the browser (for example, the separate product-admin login), without
    # forcing the upstream provider to discard its own SSO session.
    assert query["max_age"] == ["0"]
    assert "prompt" not in query
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) >= 43
    assert len(query["nonce"][0]) >= 20
    assert issued["kind"] == "browser-state-v1"
    assert issued["ttl_secs"] == 600
    payload = issued["payload"]
    assert isinstance(payload, dict)
    assert set(payload) == {
        "redirect_path",
        "provider_alias",
        "client_id",
        "code_verifier",
        "nonce",
        "browser_binding_hash",
    }
    assert payload["redirect_path"] == "/vaults?selected=one"
    assert payload["provider_alias"] == "workforce"
    assert payload["client_id"] == "akb-web"
    assert request.browser_binding not in repr(payload)
    assert payload["browser_binding_hash"] == hashlib.sha256(request.browser_binding.encode("ascii")).hexdigest()


@pytest.mark.asyncio
async def test_browser_state_is_single_use_and_exactly_browser_bound(monkeypatch):
    consumed: list[tuple[str, str]] = []

    async def consume(key, binding_hash):
        consumed.append((key, binding_hash))
        return {
            "redirect_path": "/",
            "provider_alias": "workforce",
            "client_id": "akb-web",
            "code_verifier": "v" * 43,
            "nonce": "n" * 32,
            "browser_binding_hash": binding_hash,
        }

    monkeypatch.setattr(keycloak_oidc, "_store_consume_browser_bound", consume)
    binding = "b" * 32

    state = await KeycloakOIDC().consume_browser_state("state-1", binding)

    expected_hash = hashlib.sha256(binding.encode("ascii")).hexdigest()
    assert consumed == [("state-1", expected_hash)]
    assert state is not None
    assert "browser_binding_hash" not in state
    assert await KeycloakOIDC().consume_browser_state("state-1", "short") is None


@pytest.mark.asyncio
async def test_browser_code_exchange_always_uses_pkce_and_server_client(monkeypatch):
    posted: dict[str, object] = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "token_type": "Bearer",
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": "id",
            }

    class Client:
        async def post(self, url, data):
            posted.update(url=url, data=data)
            return Response()

    service = KeycloakOIDC()
    monkeypatch.setattr(service, "_client", lambda: Client())
    monkeypatch.setattr(settings, "keycloak_public_client", False)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web")
    monkeypatch.setattr(settings, "keycloak_client_secret", "backend-only-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com")

    result = await service.exchange_browser_code("code-1", "verifier-1")

    assert result["access_token"] == "access"
    assert posted["data"] == {  # type: ignore[comparison-overlap]
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": "https://akb.example.com/api/v1/auth/keycloak/callback",
        "client_id": "akb-web",
        "code_verifier": "verifier-1",
        "client_secret": "backend-only-secret",  # pragma: allowlist secret
    }


@pytest.mark.asyncio
async def test_browser_id_token_binds_nonce_access_token_and_client(monkeypatch):
    service = KeycloakOIDC()
    access_token = "verified-access-token"
    nonce = "nonce-value-that-is-long-enough"
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    at_hash = keycloak_oidc._encode_base64url(digest[: len(digest) // 2])

    async def verified(_token, *, client_id=None):
        assert client_id == "akb-web"
        return {
            "iss": settings.keycloak_issuer,
            "sub": "user-1",
            "azp": "akb-web",
            "sid": "session-1",
            "identity_provider": "workforce",
            "nonce": nonce,
            "at_hash": at_hash,
            "iat": 100,
            "exp": 200,
        }

    monkeypatch.setattr(service, "verify_id_token", verified)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web")

    claims = await service.verify_browser_id_token(
        "signed-id-token",
        expected_nonce=nonce,
        access_token=access_token,
        expected_provider_alias="workforce",
    )

    assert claims["sid"] == "session-1"

    with pytest.raises(AuthenticationError, match="Invalid browser identity token"):
        await service.verify_browser_id_token(
            "signed-id-token",
            expected_nonce="different",
            access_token=access_token,
            expected_provider_alias="workforce",
        )

    with pytest.raises(AuthenticationError, match="Invalid browser identity token"):
        await service.verify_browser_id_token(
            "signed-id-token",
            expected_nonce=nonce,
            access_token=access_token,
            expected_provider_alias="another-provider",
        )
