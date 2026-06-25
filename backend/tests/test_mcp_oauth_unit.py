"""Unit tests for the MCP OAuth Resource Server path.

Covers the pure-function and pydantic-mockable surfaces — no DB, no
real Keycloak. Concretely:

- ``settings.mcp_oauth_audience_effective`` derives from ``public_base_url``
- ``KeycloakOIDC.verify_access_token`` accepts a freshly-signed RS256
  token, rejects wrong audience / wrong issuer / expired / wrong alg
- ``resolve_token`` dispatches on JWT ``alg``: PAT → _resolve_pat,
  HS256 → existing AKB JWT path, RS256 → Keycloak access-token path
  (gated on ``mcp_oauth_enabled``)
- ``_dispatch`` scope enforcement: ``oauth_scopes is None`` bypasses,
  missing scope returns ``insufficient_scope``, sufficient passes
- ``/.well-known/oauth-protected-resource`` shape: 404 when disabled,
  full document when enabled
- ``_www_authenticate_header`` carries ``resource_metadata`` when MCP
  OAuth is on, plain Bearer challenge when off
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings

# server.py instantiates DocumentService() at module load, which creates
# the git storage path via mkdir. Tests don't write any documents; we
# just need somewhere writable so the import doesn't blow up on the
# default `/data/vaults`. Override once at test-module load so every
# lazy `from mcp_server.server import …` inside a test finds a working
# path.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-mcp-oauth-test-vaults-")


# ── Helpers ────────────────────────────────────────────────────────


@pytest.fixture
def rsa_keypair():
    """A throwaway 2048-bit RSA keypair for signing test access tokens."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private.public_key().public_numbers()
    # JWK shape Keycloak uses for the JWKS feed.
    import base64

    def _b64u(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    jwk = {
        "kty": "RSA",
        "kid": "test-key-1",
        "use": "sig",
        "alg": "RS256",
        "n": _b64u(public_numbers.n),
        "e": _b64u(public_numbers.e),
    }
    return {"private_pem": private_pem, "jwk": jwk}


def _mint_access_token(
    *,
    private_pem: bytes,
    kid: str,
    audience: str,
    issuer: str,
    sub: str = "test-sub",
    email: str = "alice@example.com",
    email_verified: bool = True,
    preferred_username: str = "alice",
    scope: str = "akb:vault:read akb:vault:write",
    exp_delta: int = 300,
    extra: dict | None = None,
    alg: str = "RS256",
) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": sub,
        "iat": now,
        "exp": now + exp_delta,
        "email": email,
        "email_verified": email_verified,
        "preferred_username": preferred_username,
        "scope": scope,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, private_pem, algorithm=alg, headers={"kid": kid})


# ── settings.mcp_oauth_audience_effective ──────────────────────────


def test_audience_effective_off_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    assert settings.mcp_oauth_audience_effective == ""


def test_audience_effective_default_derives_from_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_audience", "", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    assert settings.mcp_oauth_audience_effective == "https://akb.example.com/mcp"


def test_audience_effective_explicit_override(monkeypatch):
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_audience", "https://alt.example.com/mcp", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    assert settings.mcp_oauth_audience_effective == "https://alt.example.com/mcp"


# ── KeycloakOIDC.verify_access_token ───────────────────────────────


@pytest.mark.asyncio
async def test_verify_access_token_happy_path(monkeypatch, rsa_keypair):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer = "https://kc.example.com/realms/akb"
    audience = "https://akb.example.com/mcp"
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)

    svc = KeycloakOIDC()
    # Inject JWKS rather than hit the network.
    svc._jwks = {"keys": [rsa_keypair["jwk"]]}

    token = _mint_access_token(
        private_pem=rsa_keypair["private_pem"],
        kid="test-key-1",
        audience=audience,
        issuer=issuer,
    )
    claims = await svc.verify_access_token(token, audience)
    assert claims is not None
    assert claims["email"] == "alice@example.com"
    assert "akb:vault:read" in claims.get("scope", "")


@pytest.mark.asyncio
async def test_verify_access_token_wrong_audience(monkeypatch, rsa_keypair):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer = "https://kc.example.com/realms/akb"
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)

    svc = KeycloakOIDC()
    svc._jwks = {"keys": [rsa_keypair["jwk"]]}

    # Token minted for resource A, validated for resource B → None.
    token = _mint_access_token(
        private_pem=rsa_keypair["private_pem"],
        kid="test-key-1",
        audience="https://other.example.com/mcp",
        issuer=issuer,
    )
    assert await svc.verify_access_token(token, "https://akb.example.com/mcp") is None


@pytest.mark.asyncio
async def test_verify_access_token_wrong_issuer(monkeypatch, rsa_keypair):
    from app.services.keycloak_oidc import KeycloakOIDC

    audience = "https://akb.example.com/mcp"
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)

    svc = KeycloakOIDC()
    svc._jwks = {"keys": [rsa_keypair["jwk"]]}

    # Token claims a different issuer than the one settings advertise.
    token = _mint_access_token(
        private_pem=rsa_keypair["private_pem"],
        kid="test-key-1",
        audience=audience,
        issuer="https://attacker.example.com/realms/akb",
    )
    assert await svc.verify_access_token(token, audience) is None


@pytest.mark.asyncio
async def test_verify_access_token_expired(monkeypatch, rsa_keypair):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer = "https://kc.example.com/realms/akb"
    audience = "https://akb.example.com/mcp"
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)

    svc = KeycloakOIDC()
    svc._jwks = {"keys": [rsa_keypair["jwk"]]}

    token = _mint_access_token(
        private_pem=rsa_keypair["private_pem"],
        kid="test-key-1",
        audience=audience,
        issuer=issuer,
        exp_delta=-60,  # already expired
    )
    assert await svc.verify_access_token(token, audience) is None


@pytest.mark.asyncio
async def test_verify_access_token_rejects_hs256(monkeypatch):
    """An HS256-signed token (AKB JWT shape) must NOT be accepted by the
    OAuth Resource Server verifier — even if alg were spoofed, the JWKS
    is RSA-only. This guards against an alg-confusion attempt."""
    from app.services.keycloak_oidc import KeycloakOIDC

    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)

    svc = KeycloakOIDC()
    svc._jwks = {"keys": []}  # irrelevant — alg check fires first

    now = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode(
        {"sub": "x", "aud": "y", "iss": "z", "iat": now, "exp": now + 60},
        "shared-secret",
        algorithm="HS256",
    )
    assert await svc.verify_access_token(token, "https://akb.example.com/mcp") is None


# ── resolve_token dispatch ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_token_routes_rs256_to_keycloak_path(monkeypatch, rsa_keypair):
    """An RS256 JWT must route to ``resolve_keycloak_access_token``, not
    fall through to the HS256 AKB JWT decoder."""
    from app.services import auth_service

    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_audience", "", raising=False)

    calls = []

    async def _stub(token):
        calls.append(token)
        return auth_service.AuthenticatedUser(
            user_id="00000000-0000-0000-0000-000000000001",
            username="alice",
            email="alice@example.com",
            display_name="Alice",
            is_admin=False,
            auth_method="oauth",
            oauth_scopes=["akb:vault:read"],
        )

    monkeypatch.setattr(auth_service, "resolve_keycloak_access_token", _stub)

    token = _mint_access_token(
        private_pem=rsa_keypair["private_pem"],
        kid="test-key-1",
        audience="https://akb.example.com/mcp",
        issuer="https://kc.example.com/realms/akb",
    )
    result = await auth_service.resolve_token(f"Bearer {token}")
    assert result is not None
    assert result.auth_method == "oauth"
    assert result.oauth_scopes == ["akb:vault:read"]
    assert calls == [token]


@pytest.mark.asyncio
async def test_resolve_token_pat_unchanged(monkeypatch):
    """The PAT prefix path must not be perturbed by the new RS256 branch."""
    from app.services import auth_service

    seen = []

    async def _stub(t):
        seen.append(t)
        return None  # we only care that the PAT path is taken

    monkeypatch.setattr(auth_service, "_resolve_pat", _stub)
    await auth_service.resolve_token("Bearer akb_some_pat_value")
    assert seen == ["akb_some_pat_value"]


@pytest.mark.asyncio
async def test_resolve_token_rs256_rejected_when_mcp_oauth_off(monkeypatch, rsa_keypair):
    """Resource-server gating: with ``mcp_oauth_enabled = false`` an
    RS256 token is rejected to None, not silently honoured."""
    from app.services import auth_service

    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    token = _mint_access_token(
        private_pem=rsa_keypair["private_pem"],
        kid="test-key-1",
        audience="https://akb.example.com/mcp",
        issuer="https://kc.example.com/realms/akb",
    )
    assert await auth_service.resolve_token(f"Bearer {token}") is None


# ── _dispatch scope enforcement ────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_skips_scope_check_when_oauth_scopes_none():
    """PAT and AKB JWT callers carry ``oauth_scopes is None`` and must
    NOT be gated by the OAuth scope check — only by PG-RBAC downstream."""
    from mcp_server.server import _dispatch, _MCPUser, _HANDLERS

    captured = []

    async def _stub_handler(args, uid, user):
        captured.append((uid, args))
        return {"ok": True}

    _HANDLERS["__test_tool__"] = _stub_handler
    try:
        user = _MCPUser(user_id="u-1", oauth_scopes=None)
        result = await _dispatch("__test_tool__", {}, user)
        assert result == {"ok": True}
        assert captured == [("u-1", {})]
    finally:
        _HANDLERS.pop("__test_tool__", None)


@pytest.mark.asyncio
async def test_dispatch_insufficient_scope_for_write_with_only_read():
    """An OAuth caller with ``akb:vault:read`` must be refused at a
    write-grade tool with the canonical ``insufficient_scope`` code."""
    from mcp_server.server import _dispatch, _MCPUser

    user = _MCPUser(user_id="u-1", oauth_scopes=["akb:vault:read"])
    result = await _dispatch("akb_put", {"vault": "v", "title": "t", "content": "c"}, user)
    assert result.get("error") is not None
    assert result.get("code") == "insufficient_scope"
    # `err()` wraps arbitrary kwargs under `details`, so the per-tool
    # scope hints surface there rather than at the top level.
    assert result.get("details", {}).get("required_scope") == "akb:vault:write"
    assert result.get("details", {}).get("granted_scopes") == ["akb:vault:read"]


@pytest.mark.asyncio
async def test_dispatch_sufficient_scope_passes_through(monkeypatch):
    """With both scopes present, the dispatch must run the handler (we
    stub it so this stays a unit test, not a DB-backed integration)."""
    from mcp_server.server import _dispatch, _MCPUser, _HANDLERS

    called = []

    async def _stub(args, uid, user):
        called.append(user.oauth_scopes)
        return {"ok": True}

    original = _HANDLERS.get("akb_put")
    _HANDLERS["akb_put"] = _stub
    try:
        user = _MCPUser(user_id="u-1", oauth_scopes=["akb:vault:read", "akb:vault:write"])
        result = await _dispatch("akb_put", {"vault": "v", "title": "t", "content": "c"}, user)
        assert result == {"ok": True}
        assert called == [["akb:vault:read", "akb:vault:write"]]
    finally:
        if original is not None:
            _HANDLERS["akb_put"] = original
        else:
            _HANDLERS.pop("akb_put", None)


@pytest.mark.asyncio
async def test_dispatch_empty_oauth_scopes_rejects_scoped_tool():
    """A caller authenticated via OAuth but with zero scopes (token was
    minted without any vault scopes requested) must be refused at any
    tool that has a scope mapping. Empty list ≠ None."""
    from mcp_server.server import _dispatch, _MCPUser

    user = _MCPUser(user_id="u-1", oauth_scopes=[])
    result = await _dispatch("akb_search", {"query": "x"}, user)
    assert result.get("code") == "insufficient_scope"
    assert result.get("details", {}).get("required_scope") == "akb:vault:read"


# ── /.well-known/oauth-protected-resource ──────────────────────────


def test_metadata_route_404_when_disabled(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    from app.api.routes import oauth_metadata

    app = FastAPI()
    app.include_router(oauth_metadata.router)
    client = TestClient(app)
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 404


def test_metadata_route_full_shape_when_enabled(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_audience", "", raising=False)

    from app.api.routes import oauth_metadata

    app = FastAPI()
    app.include_router(oauth_metadata.router)
    client = TestClient(app)
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://akb.example.com/mcp"
    assert body["authorization_servers"] == ["https://kc.example.com/realms/akb"]
    assert "akb:vault:read" in body["scopes_supported"]
    assert "akb:vault:write" in body["scopes_supported"]
    assert "offline_access" in body["scopes_supported"]
    # OIDC base scopes are also advertised so spec-compliant MCP
    # clients (which request exactly scopes_supported) include them in
    # DCR + authorize. Without these the access token carries `sub`
    # only and AKB's email-keyed user matching falls through.
    assert "openid" in body["scopes_supported"]
    assert "profile" in body["scopes_supported"]
    assert "email" in body["scopes_supported"]
    assert body["bearer_methods_supported"] == ["header"]


# ── WWW-Authenticate header ────────────────────────────────────────


def test_www_authenticate_plain_bearer_when_oauth_off(monkeypatch):
    from mcp_server.http_app import _www_authenticate_header

    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    assert _www_authenticate_header() == 'Bearer realm="akb-mcp"'


def test_www_authenticate_carries_resource_metadata_when_oauth_on(monkeypatch):
    from mcp_server.http_app import _www_authenticate_header

    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    h = _www_authenticate_header()
    assert h.startswith("Bearer realm=")
    assert 'resource_metadata="https://akb.example.com/.well-known/oauth-protected-resource"' in h


# ── _TOOL_SCOPES completeness (CI guard) ──────────────────────────


def test_every_registered_tool_has_an_explicit_scope_mapping():
    """A new tool added to `_HANDLERS` without an entry in `_TOOL_SCOPES`
    would silently fail closed to write (which is safe) — but the
    intent is that every tool's scope is an explicit, reviewed choice.
    Lock that down here so a PR adding a read-grade tool can't
    inadvertently promote it to write-grade by forgetting the map."""
    from mcp_server.server import _HANDLERS, _TOOL_SCOPES

    unmapped = sorted(set(_HANDLERS.keys()) - set(_TOOL_SCOPES.keys()))
    assert unmapped == [], (
        f"Tools registered without a _TOOL_SCOPES entry: {unmapped}. "
        "Add each to _TOOL_SCOPES with the appropriate read/write scope."
    )


@pytest.mark.asyncio
async def test_unmapped_tool_falls_back_to_write_scope_when_oauth_caller():
    """A defensive check for the fail-closed path: even with the
    completeness test above in place, if a future tool slips through
    (test bypass / dynamic registration), an OAuth caller without
    `akb:vault:write` must NOT be able to invoke it."""
    from mcp_server.server import _dispatch, _MCPUser, _HANDLERS

    # Register a fake tool that's deliberately NOT in _TOOL_SCOPES.
    async def _stub(args, uid, user):
        return {"ok": True}

    _HANDLERS["__unmapped_test_tool__"] = _stub
    try:
        # Read-only OAuth caller — must be refused at unmapped tool.
        read_user = _MCPUser(user_id="u-1", oauth_scopes=["akb:vault:read"])
        result = await _dispatch("__unmapped_test_tool__", {}, read_user)
        assert result.get("code") == "insufficient_scope"
        assert result.get("details", {}).get("required_scope") == "akb:vault:write"
        # Write-grade OAuth caller — allowed through.
        write_user = _MCPUser(user_id="u-1", oauth_scopes=["akb:vault:write"])
        result = await _dispatch("__unmapped_test_tool__", {}, write_user)
        assert result == {"ok": True}
    finally:
        _HANDLERS.pop("__unmapped_test_tool__", None)


# ── verify_access_token resilience ─────────────────────────────────


@pytest.mark.asyncio
async def test_verify_access_token_returns_none_on_jwks_unreachable(monkeypatch, rsa_keypair):
    """JWKS endpoint flap must not surface as 502 — return None so the
    MCP handler issues a clean 401 with the RFC 9728 hint and the
    client retries discovery."""
    from app.services.keycloak_oidc import KeycloakOIDC
    from app.exceptions import AKBError

    issuer = "https://kc.example.com/realms/akb"
    audience = "https://akb.example.com/mcp"
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)

    svc = KeycloakOIDC()
    # Stub _fetch_jwks to simulate "Keycloak unreachable".
    async def _boom(*_a, **_kw):
        raise AKBError("Keycloak unreachable fetching JWKS", status_code=502)
    monkeypatch.setattr(svc, "_fetch_jwks", _boom)

    token = _mint_access_token(
        private_pem=rsa_keypair["private_pem"],
        kid="test-key-1",
        audience=audience,
        issuer=issuer,
    )
    assert await svc.verify_access_token(token, audience) is None


# ── SPA-audience token must not be usable at /mcp ──────────────────


@pytest.mark.asyncio
async def test_verify_rejects_spa_audience_token(monkeypatch, rsa_keypair):
    """The existing `akb-web` SSO client mints ID tokens with
    aud=akb-web for the browser login flow. Those tokens must NOT be
    accepted at /mcp — the audience binding is what stops cross-client
    confusion."""
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer = "https://kc.example.com/realms/akb"
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)

    svc = KeycloakOIDC()
    svc._jwks = {"keys": [rsa_keypair["jwk"]]}

    # Token minted with the SPA's audience (the client_id).
    spa_token = _mint_access_token(
        private_pem=rsa_keypair["private_pem"],
        kid="test-key-1",
        audience="akb-web",
        issuer=issuer,
    )
    # Validated against the MCP resource audience → rejected.
    assert await svc.verify_access_token(spa_token, "https://akb.example.com/mcp") is None
