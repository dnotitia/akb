"""Unit coverage for per-companion Keycloak OIDC client selection.

The selected client is server-owned flow state.  It must be identical at the
authorization endpoint, token exchange, and ID-token audience check; otherwise
a themed companion login either leaks onto the shared client or fails midway.
"""
from __future__ import annotations

import urllib.parse

import pytest

from app.config import settings
from app.services import keycloak_oidc
from app.services.keycloak_oidc import KeycloakOIDC


@pytest.mark.asyncio
async def test_begin_login_persists_and_authorizes_with_selected_client(monkeypatch):
    issued: dict[str, object] = {}

    async def capture_issue(key, kind, payload, ttl_secs):
        issued.update(key=key, kind=kind, payload=payload, ttl_secs=ttl_secs)

    monkeypatch.setattr(keycloak_oidc, "_store_issue", capture_issue)
    monkeypatch.setattr(settings, "keycloak_public_client", False)

    url = await KeycloakOIDC().begin_login(
        "https://naut.example.com/api/auth/akb/sso/callback",
        client_id="naut-web",
    )

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["client_id"] == ["naut-web"]
    assert issued["kind"] == "state"
    assert issued["payload"] == {
        "redirect_path": "https://naut.example.com/api/auth/akb/sso/callback",
        "client_id": "naut-web",
    }


@pytest.mark.asyncio
async def test_token_exchange_uses_selected_client_with_backend_secret(monkeypatch):
    posted: dict[str, object] = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"id_token": "signed"}

    class Client:
        async def post(self, url, data):
            posted.update(url=url, data=data)
            return Response()

    service = KeycloakOIDC()
    monkeypatch.setattr(service, "_client", lambda: Client())
    monkeypatch.setattr(settings, "keycloak_public_client", False)
    monkeypatch.setattr(settings, "keycloak_client_secret", "backend-only-secret")

    result = await service.exchange_code_for_tokens(
        "code-1", None, client_id="naut-web"
    )

    assert result == {"id_token": "signed"}
    assert posted["data"]["client_id"] == "naut-web"  # type: ignore[index]
    assert posted["data"]["client_secret"] == "backend-only-secret"  # type: ignore[index]


@pytest.mark.asyncio
async def test_id_token_audience_uses_selected_client(monkeypatch):
    decoded: dict[str, object] = {}
    service = KeycloakOIDC()

    async def fake_jwks(*, force=False):
        return {
            "keys": [
                {"kid": "key-1", "kty": "RSA", "use": "sig", "alg": "RS256"}
            ]
        }

    def fake_decode(token, key, **kwargs):
        decoded.update(token=token, key=key, **kwargs)
        return {"sub": "user-1"}

    monkeypatch.setattr(service, "_fetch_jwks", fake_jwks)
    monkeypatch.setattr(
        keycloak_oidc.jwt,
        "get_unverified_header",
        lambda _: {"kid": "key-1", "alg": "RS256", "typ": "JWT"},
    )
    monkeypatch.setattr(keycloak_oidc.RSAAlgorithm, "from_jwk", lambda _: "public-key")
    monkeypatch.setattr(keycloak_oidc.jwt, "decode", fake_decode)

    claims = await service.verify_id_token("signed", client_id="naut-web")

    assert claims == {"sub": "user-1"}
    assert decoded["audience"] == "naut-web"


@pytest.mark.asyncio
async def test_default_client_remains_the_global_client(monkeypatch):
    issued: dict[str, object] = {}

    async def capture_issue(_key, _kind, payload, ttl_secs):
        assert ttl_secs == 600
        issued.update(payload)

    monkeypatch.setattr(keycloak_oidc, "_store_issue", capture_issue)
    monkeypatch.setattr(settings, "keycloak_public_client", False)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web")

    url = await KeycloakOIDC().begin_login("/dashboard")

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["client_id"] == ["akb-web"]
    assert issued["client_id"] == "akb-web"
