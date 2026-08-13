"""Unit tests for the MCP OAuth Resource Server path.

Covers the pure-function and pydantic-mockable surfaces — no DB, no
real Keycloak. Concretely:

- ``settings.mcp_oauth_audience_effective`` derives from ``public_base_url``
- ``KeycloakOIDC.verify_access_token`` returns a typed principal for RS256
  token, rejects wrong audience / wrong issuer / expired / wrong alg
- the MCP capability selects PAT/service-by-prefix or keycloak-access-v1;
  local session JWTs are not an MCP credential
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

# server.py selects the process-scoped DocumentService at module load, whose
# legacy implementation creates the git storage path via mkdir. Tests don't
# write documents; we just need somewhere writable so lazy imports do not
# target the default `/data/vaults`.
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
        "jti": "test-jti",
        "typ": "Bearer",
        "azp": "test-browser-client",
        "sid": "test-human-session",
        "email": email,
        "email_verified": email_verified,
        "preferred_username": preferred_username,
        "scope": scope,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(
        payload,
        private_pem,
        algorithm=alg,
        headers={"kid": kid, "typ": "JWT"},
    )


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
    principal = await svc.verify_access_token(token, audience, route_profile="mcp")
    assert principal is not None
    assert principal.claims["email"] == "alice@example.com"
    assert "akb:vault:read" in principal.claims["scope"]


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
    assert (
        await svc.verify_access_token(
            token,
            "https://akb.example.com/mcp",
            route_profile="mcp",
        )
        is None
    )


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
    assert await svc.verify_access_token(token, audience, route_profile="mcp") is None


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
    assert await svc.verify_access_token(token, audience, route_profile="mcp") is None


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
        "shared-secret-at-least-32-bytes-long",
        algorithm="HS256",
    )
    assert (
        await svc.verify_access_token(
            token,
            "https://akb.example.com/mcp",
            route_profile="mcp",
        )
        is None
    )


# ── MCP capability selection ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_mcp_selects_keycloak_profile_without_alg_dispatch(monkeypatch):
    from app.services import auth_service
    from app.services.auth_verifier_profiles import VerifiedPrincipal

    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_audience", "", raising=False)

    calls: list[tuple[str, str]] = []
    actor = auth_service.AuthenticatedUser(
        user_id="00000000-0000-0000-0000-000000000001",
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        is_admin=False,
        auth_method="oauth",
        oauth_scopes=["akb:vault:read"],
    )
    principal = VerifiedPrincipal(
        profile_id="keycloak-access-v1",
        issuer="https://kc.example.com/realms/akb",
        subject="alice-subject",
        credential_type="access_token",
        claims={"scope": "akb:vault:read"},
        audience="https://akb.example.com/mcp",
    )

    async def verify(token: str, route_profile: str):
        calls.append((token, route_profile))
        return principal

    async def project(value):
        assert value is principal
        return actor

    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", verify)
    monkeypatch.setattr(auth_service, "project_verified_principal", project)
    result = await auth_service.resolve_mcp_authorization("Bearer opaque.jwt")
    assert result is not None
    assert result.auth_method == "oauth"
    assert result.oauth_scopes == ["akb:vault:read"]
    assert calls == [("opaque.jwt", "mcp")]


@pytest.mark.asyncio
async def test_mcp_oauth_maps_suspended_external_account_to_auth_failure(monkeypatch):
    from app.exceptions import AccountSuspendedError
    from app.services import auth_service
    from app.services.auth_verifier_profiles import VerifiedPrincipal

    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(
        settings,
        "mcp_oauth_audience",
        "https://akb.example.com/mcp",
        raising=False,
    )

    principal = VerifiedPrincipal(
        profile_id="keycloak-access-v1",
        issuer="https://kc.example.com/realms/akb",
        subject="suspended-subject",
        credential_type="access_token",
        claims={
            "iss": "https://kc.example.com/realms/akb",
            "sub": "suspended-subject",
            "scope": "akb:vault:read",
        },
        audience="https://akb.example.com/mcp",
    )

    async def verify(_token: str, _route_profile: str):
        return principal

    async def _suspended(_claims):
        raise AccountSuspendedError()

    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", verify)
    monkeypatch.setattr(auth_service, "_resolve_or_provision_keycloak_user", _suspended)

    assert await auth_service.resolve_mcp_authorization("Bearer signed-token") is None


@pytest.mark.asyncio
async def test_resolve_mcp_pat_unchanged(monkeypatch):
    from app.services import auth_service

    seen = []

    async def _stub(t):
        seen.append(t)
        return None  # we only care that the PAT path is taken

    monkeypatch.setattr(auth_service, "_resolve_pat", _stub)
    await auth_service.resolve_mcp_authorization("Bearer akb_some_pat_value")
    assert seen == ["akb_some_pat_value"]


@pytest.mark.asyncio
async def test_resolve_mcp_rs256_rejected_when_mcp_oauth_off(monkeypatch, rsa_keypair):
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
    assert await auth_service.resolve_mcp_authorization(f"Bearer {token}") is None


# ── _dispatch scope enforcement ────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_skips_scope_check_when_oauth_scopes_none():
    """PAT and service callers carry ``oauth_scopes is None`` and must
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


@pytest.mark.asyncio
async def test_dispatch_read_only_pat_scope_rejects_write_tool():
    """A PAT/service token with only the coarse `read` scope cannot call
    write-grade MCP tools."""
    from mcp_server.server import _dispatch, _MCPUser

    user = _MCPUser(user_id="u-1", token_scopes=frozenset({"read"}))
    result = await _dispatch("akb_put", {"vault": "v", "title": "t", "content": "c"}, user)
    assert result.get("code") == "insufficient_scope"
    assert result.get("details", {}).get("required_scope") == "write"
    assert result.get("details", {}).get("granted_scopes") == ["read"]


def test_publication_snapshot_is_write_scoped():
    from mcp_server.server import _TOOL_SCOPES, _WRITE_SCOPE

    assert _TOOL_SCOPES["akb_publication_snapshot"] == _WRITE_SCOPE


@pytest.mark.asyncio
async def test_dispatch_read_only_pat_scope_allows_read_tool():
    from mcp_server.server import _dispatch, _MCPUser, _HANDLERS

    called = []

    async def _stub(args, uid, user):
        called.append((args, uid, user.token_scopes))
        return {"ok": True}

    original = _HANDLERS.get("akb_search")
    _HANDLERS["akb_search"] = _stub
    try:
        user = _MCPUser(user_id="u-1", token_scopes=frozenset({"read"}))
        result = await _dispatch("akb_search", {"query": "x"}, user)
        assert result == {"ok": True}
        assert called == [({"query": "x"}, "u-1", frozenset({"read"}))]
    finally:
        if original is not None:
            _HANDLERS["akb_search"] = original
        else:
            _HANDLERS.pop("akb_search", None)


# ── akb_grep: scope depends on the arguments, not just the tool ────
#
# Rationale lives with the rule, at `_ARG_WRITE_TRIGGERS` in server.py.


@pytest.mark.asyncio
async def test_grep_with_replace_is_refused_for_read_only_oauth_caller():
    from mcp_server.server import _dispatch, _MCPUser

    user = _MCPUser(user_id="u-1", oauth_scopes=["akb:vault:read"])
    result = await _dispatch("akb_grep", {"vault": "v", "pattern": "x", "replace": "y"}, user)
    assert result.get("code") == "insufficient_scope"
    assert result.get("details", {}).get("required_scope") == "akb:vault:write"


@pytest.mark.asyncio
async def test_grep_with_replace_is_refused_for_read_only_pat():
    from mcp_server.server import _dispatch, _MCPUser

    user = _MCPUser(user_id="u-1", token_scopes=frozenset({"read"}))
    result = await _dispatch("akb_grep", {"vault": "v", "pattern": "x", "replace": "y"}, user)
    assert result.get("code") == "insufficient_scope"
    assert result.get("details", {}).get("required_scope") == "write"


@pytest.mark.asyncio
async def test_grep_without_replace_stays_readable_for_read_only_pat(monkeypatch):
    """Non-regression: plain grep must remain available to read-only
    tokens — promoting the whole tool to write-grade would have been the
    blunt fix and would take literal search away from every read agent."""
    from mcp_server.server import _dispatch, _MCPUser, _HANDLERS

    called = []

    async def _stub(args, uid, user):
        called.append(args)
        return {"ok": True}

    monkeypatch.setitem(_HANDLERS, "akb_grep", _stub)
    user = _MCPUser(user_id="u-1", token_scopes=frozenset({"read"}))
    result = await _dispatch("akb_grep", {"vault": "v", "pattern": "x"}, user)
    assert result == {"ok": True}
    assert called == [{"vault": "v", "pattern": "x"}]


@pytest.mark.asyncio
async def test_grep_with_replace_passes_when_write_scope_present(monkeypatch):
    from mcp_server.server import _dispatch, _MCPUser, _HANDLERS

    called = []

    async def _stub(args, uid, user):
        called.append(args)
        return {"ok": True}

    monkeypatch.setitem(_HANDLERS, "akb_grep", _stub)
    user = _MCPUser(user_id="u-1", token_scopes=frozenset({"read", "write"}))
    args = {"vault": "v", "pattern": "x", "replace": "y"}
    assert await _dispatch("akb_grep", args, user) == {"ok": True}
    assert called == [args]


def test_arg_sensitive_scope_rule_is_declared_not_hardcoded_in_dispatch():
    """The rule must live in one reviewable place so the next tool that
    grows a mutating argument has somewhere obvious to declare it."""
    from mcp_server.server import _WRITE_SCOPE, _required_scope

    assert _required_scope("akb_grep", {"pattern": "x"}) != _WRITE_SCOPE
    assert _required_scope("akb_grep", {"pattern": "x", "replace": ""}) == _WRITE_SCOPE


def test_every_arg_write_trigger_names_a_real_tool_argument():
    """A typo in a trigger name would silently disable the promotion —
    the failure mode this whole block exists to prevent. Checked against
    `_TOOL_ARG_NAMES`, which is the same schema-derived table `_dispatch`
    rejects unknown arguments with, so a trigger can never name an
    argument the dispatcher would refuse anyway."""
    from mcp_server.server import _ARG_WRITE_TRIGGERS, _TOOL_ARG_NAMES

    for tool, triggers in _ARG_WRITE_TRIGGERS.items():
        assert tool in _TOOL_ARG_NAMES, f"_ARG_WRITE_TRIGGERS names unknown tool {tool!r}"
        for arg in triggers:
            assert arg in _TOOL_ARG_NAMES[tool], (
                f"_ARG_WRITE_TRIGGERS[{tool!r}] names {arg!r}, which is not an argument of {tool}"
            )


def _handler_can_write(handler) -> bool:
    """True when the handler reaches a writer-gated access check.

    AST rather than a substring scan: `'required_role="writer"' in source`
    is defeated by single quotes, a module constant, or an enum — none of
    which this repo lints against — so the guard below would have gone
    green on exactly the regression it exists to catch. A non-literal
    `required_role` is treated as a write (fail closed).

    Still source-level, so a handler that delegates its access check to a
    helper is a false negative. Tripwire, not proof.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if fname != "check_vault_access":
            continue
        for kw in node.keywords:
            if kw.arg != "required_role":
                continue
            if not isinstance(kw.value, ast.Constant):
                return True  # non-literal role — cannot prove it is read-only
            if kw.value.value != "reader":
                return True
    return False


def test_the_omission_guard_itself_is_not_vacuous():
    """`_handler_can_write` is the whole force of the guard below, and a
    silently-broken predicate would make it pass forever. Pin it against
    the one handler known to write."""
    from mcp_server.server import _HANDLERS

    assert _handler_can_write(_HANDLERS["akb_grep"]) is True
    assert _handler_can_write(_HANDLERS["akb_search"]) is False


def test_read_scoped_tools_that_can_write_declare_an_arg_trigger():
    """The omission guard — the failure mode that actually recurs.

    `_TOOL_SCOPES` has a completeness guard (a new tool must be mapped);
    `_ARG_WRITE_TRIGGERS` needs the mirror of it, or the next read-mapped
    tool that grows a mutating argument reproduces this bug silently.

    The invariant is checkable against code that already exists: a
    handler that can reach a writer-gated `check_vault_access` performs a
    write, so its tool cannot be read-grade unless an argument promotes
    it.
    """
    from mcp_server.server import (
        _ARG_WRITE_TRIGGERS,
        _HANDLERS,
        _READ_SCOPE,
        _TOOL_SCOPES,
    )

    offenders = sorted(
        name
        for name, handler in _HANDLERS.items()
        if _TOOL_SCOPES.get(name) == _READ_SCOPE and name not in _ARG_WRITE_TRIGGERS and _handler_can_write(handler)
    )
    assert offenders == [], (
        f"Read-scoped tools whose handler performs a writer-gated write, with no "
        f"_ARG_WRITE_TRIGGERS entry: {offenders}. Either the tool belongs in the "
        f"write half of _TOOL_SCOPES, or the argument that makes it write must be "
        f"declared in _ARG_WRITE_TRIGGERS."
    )


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
    # DCR + authorize. Open-mode first-login fallback still needs email;
    # exact issuer/subject bindings do not.
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
async def test_verify_access_token_preserves_jwks_unreachable(monkeypatch, rsa_keypair):
    """A pinned-JWKS outage is availability failure, not bad credentials."""
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
    with pytest.raises(AKBError) as captured:
        await svc.verify_access_token(token, audience, route_profile="mcp")

    assert captured.value.status_code == 502


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
    assert (
        await svc.verify_access_token(
            spa_token,
            "https://akb.example.com/mcp",
            route_profile="mcp",
        )
        is None
    )
