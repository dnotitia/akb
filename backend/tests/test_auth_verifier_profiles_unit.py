"""Route-selected, versioned human-verifier profile contracts."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _replace_header(token: str, **updates: object) -> str:
    encoded_header, payload, signature = token.split(".")
    padded = encoded_header + "=" * (-len(encoded_header) % 4)
    header = json.loads(base64.urlsafe_b64decode(padded))
    header.update(updates)
    return ".".join((_b64url(json.dumps(header, separators=(",", ":")).encode()), payload, signature))


@pytest.fixture
def rsa_keypair() -> dict[str, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private.public_key().public_numbers()

    def encode_number(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return _b64url(value.to_bytes(length, "big"))

    return {
        "private_pem": private_pem,
        "jwk": {
            "kty": "RSA",
            "kid": "profile-key-1",
            "use": "sig",
            "alg": "RS256",
            "n": encode_number(public_numbers.n),
            "e": encode_number(public_numbers.e),
        },
    }


def _keycloak_claims(
    *,
    issuer: str,
    audience: str | list[str],
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    now = int(datetime.now(timezone.utc).timestamp())
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": "human-subject-1",
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "typ": "Bearer",
        "azp": "browser-client",
        "sid": str(uuid.uuid4()),
        "session_state": str(uuid.uuid4()),
        "auth_time": now - 30,
        "acr": "1",
        "scope": "openid profile email akb:vault:read",
        "preferred_username": "alice",
        "email": "alice@example.com",
        "email_verified": True,
        "name": "Alice",
        "realm_access": {"roles": ["default-roles-akb"]},
        "resource_access": {"account": {"roles": ["view-profile"]}},
    }
    if overrides:
        for name, value in overrides.items():
            if value is _MISSING:
                claims.pop(name, None)
            else:
                claims[name] = value
    return claims


_MISSING = object()


def _mint_keycloak_token(
    rsa_keypair: dict[str, Any],
    *,
    issuer: str,
    audience: str | list[str],
    claim_overrides: dict[str, object] | None = None,
    header_overrides: dict[str, object] | None = None,
) -> str:
    headers: dict[str, object] = {"kid": "profile-key-1", "typ": "JWT"}
    if header_overrides:
        headers.update(header_overrides)
    return jwt.encode(
        _keycloak_claims(
            issuer=issuer,
            audience=audience,
            overrides=claim_overrides,
        ),
        rsa_keypair["private_pem"],
        algorithm="RS256",
        headers=headers,
    )


def _configure_keycloak(monkeypatch) -> tuple[str, str, str]:
    issuer = "https://identity.example.com/realms/akb"
    api_audience = "https://akb.example.com/api"
    mcp_audience = "https://akb.example.com/mcp"
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://identity.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_client_id", "browser-client", raising=False)
    monkeypatch.setattr(
        settings,
        "keycloak_companion_client_ids_by_origin",
        {"https://companion.example.com": "companion-client"},
        raising=False,
    )
    monkeypatch.setattr(settings, "api_oauth_audience", api_audience, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_audience", mcp_audience, raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    return issuer, api_audience, mcp_audience


def _actor(auth_method: str = "jwt"):
    from app.services.auth_service import AuthenticatedUser

    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        is_admin=False,
        auth_method=auth_method,
    )


def _principal(profile_id: str):
    from app.services.auth_verifier_profiles import VerifiedPrincipal

    return VerifiedPrincipal(
        profile_id=profile_id,
        issuer="akb-local" if profile_id.startswith("local-") else "https://identity.example.com/realms/akb",
        subject=str(uuid.uuid4()),
        credential_type="session" if profile_id.startswith("local-") else "access_token",
        claims={"iat": 1},
        audience=None,
    )


@pytest.mark.asyncio
async def test_rest_local_mode_selects_only_local_profile_without_fallback(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    principal = _principal("local-session-legacy-v1")
    actor = _actor()
    calls: list[str] = []

    def verify_local(token: str):
        calls.append(f"local:{token}")
        return principal

    async def forbidden_keycloak(*_args, **_kwargs):
        raise AssertionError("local REST must not try the Keycloak verifier")

    async def project(value):
        assert value is principal
        return actor

    monkeypatch.setattr(auth_service, "verify_local_session_legacy_v1", verify_local)
    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", forbidden_keycloak)
    monkeypatch.setattr(auth_service, "project_verified_principal", project)

    resolved = await auth_service.resolve_rest_user_authorization("Bearer opaque.jwt")

    assert resolved is actor
    assert calls == ["local:opaque.jwt"]

    monkeypatch.setattr(auth_service, "verify_local_session_legacy_v1", lambda _token: None)
    assert await auth_service.resolve_rest_user_authorization("Bearer rejected.jwt") is None


@pytest.mark.asyncio
async def test_rest_sso_mode_selects_only_api_access_profile_without_fallback(monkeypatch):
    from app.services import auth_service

    _configure_keycloak(monkeypatch)
    principal = _principal("keycloak-access-v1")
    actor = _actor("oauth")
    calls: list[tuple[str, str]] = []

    def forbidden_local(_token: str):
        raise AssertionError("SSO REST must not try the local verifier")

    async def verify_keycloak(token: str, route_profile: str):
        calls.append((token, route_profile))
        return principal

    async def project(value):
        assert value is principal
        return actor

    monkeypatch.setattr(auth_service, "verify_local_session_legacy_v1", forbidden_local)
    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", verify_keycloak)
    monkeypatch.setattr(auth_service, "project_verified_principal", project)

    resolved = await auth_service.resolve_rest_user_authorization("Bearer opaque.jwt")

    assert resolved is actor
    assert calls == [("opaque.jwt", "api")]

    async def reject_keycloak(_token: str, _route_profile: str):
        return None

    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", reject_keycloak)
    assert await auth_service.resolve_rest_user_authorization("Bearer rejected.jwt") is None


@pytest.mark.asyncio
async def test_rest_sso_mode_rejects_a_valid_local_session_jwt(monkeypatch):
    from app.services import auth_service
    from app.services.auth_verifier_profiles import verify_local_session_legacy_v1
    from app.services.keycloak_oidc import KeycloakOIDC

    secret = "required-compatibility-hmac-secret"  # pragma: allowlist secret
    now = int(datetime.now(timezone.utc).timestamp())
    local_token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "username": "local-alice",
            "iat": now,
            "exp": now + 300,
        },
        secret,
        algorithm="HS256",
        headers={"typ": "JWT"},
    )
    monkeypatch.setattr(settings, "jwt_secret", secret, raising=False)
    assert verify_local_session_legacy_v1(local_token) is not None

    _, api_audience, _ = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()

    async def forbidden_fetch(*, force: bool = False):
        raise AssertionError(f"HS256 local token must fail before JWKS fetch: {force}")

    async def verify_keycloak(token: str, route_profile: str):
        assert route_profile == "api"
        return await service.verify_access_token(
            token,
            api_audience,
            route_profile="api",
        )

    def forbidden_local(_token: str):
        raise AssertionError("SSO REST must never invoke the local-session verifier")

    async def forbidden_projection(_principal):
        raise AssertionError("a local session must be rejected before projection")

    service._fetch_jwks = forbidden_fetch  # type: ignore[method-assign]
    monkeypatch.setattr(auth_service, "verify_local_session_legacy_v1", forbidden_local)
    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", verify_keycloak)
    monkeypatch.setattr(auth_service, "project_verified_principal", forbidden_projection)

    assert await auth_service.resolve_rest_user_authorization(f"Bearer {local_token}") is None


@pytest.mark.asyncio
async def test_delegated_human_resolver_is_mode_selected_and_preserves_primary_context(
    monkeypatch,
):
    from app.models.vault_scope import current_key_class, current_token_id
    from app.services import auth_service

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    principal = _principal("local-session-legacy-v1")
    actor = _actor()
    monkeypatch.setattr(auth_service, "verify_local_session_legacy_v1", lambda _token: principal)

    async def project(_principal):
        return actor

    monkeypatch.setattr(auth_service, "project_verified_principal", project)
    token_marker = current_token_id.set("primary-token")
    class_marker = current_key_class.set("service")
    try:
        resolved = await auth_service.resolve_delegated_human_authorization("Bearer delegated.jwt")
        assert resolved is actor
        assert current_token_id.get() == "primary-token"
        assert current_key_class.get() == "service"
        assert await auth_service.resolve_delegated_human_authorization("Bearer akb_pat") is None
    finally:
        current_key_class.reset(class_marker)
        current_token_id.reset(token_marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "key_class"),
    [("akb_real_pat", "pat"), ("akb_secret_real_service", "service")],
)
async def test_rest_and_mcp_accept_only_namespaced_token_store_credentials(
    monkeypatch,
    raw: str,
    key_class: str,
):
    from app.services import auth_service

    actor = _actor("pat")
    actor.key_class = key_class
    calls: list[str] = []

    async def resolve_pat(token: str):
        calls.append(token)
        return actor

    monkeypatch.setattr(auth_service, "_resolve_pat", resolve_pat)
    assert await auth_service.resolve_rest_user_authorization(f"Bearer {raw}") is actor
    assert await auth_service.resolve_mcp_authorization(f"Bearer {raw}") is actor
    assert calls == [raw, raw]


@pytest.mark.asyncio
async def test_reserved_token_store_class_is_not_a_rest_or_mcp_credential(monkeypatch):
    from app.services import auth_service

    reserved = _actor("pat")
    reserved.key_class = "publishable"

    async def resolve_reserved(_token: str):
        return reserved

    monkeypatch.setattr(auth_service, "_resolve_pat", resolve_reserved)

    assert await auth_service.resolve_rest_user_authorization("Bearer akb_reserved") is None
    assert await auth_service.resolve_mcp_authorization("Bearer akb_reserved") is None


@pytest.mark.asyncio
async def test_mcp_never_accepts_local_session_jwt(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", False, raising=False)

    def forbidden_local(_token: str):
        raise AssertionError("MCP must not invoke the local-session verifier")

    monkeypatch.setattr(auth_service, "verify_local_session_legacy_v1", forbidden_local)
    assert await auth_service.resolve_mcp_authorization("Bearer local.session.jwt") is None


def test_local_session_legacy_v1_is_fixed_hs256_with_strict_current_claims(monkeypatch):
    from app.services.auth_verifier_profiles import verify_local_session_legacy_v1

    secret = "local-session-test-secret-at-least-32-bytes"  # pragma: allowlist secret
    monkeypatch.setattr(settings, "jwt_secret", secret, raising=False)
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "sub": str(uuid.uuid4()),
        "username": "alice",
        "iat": now,
        "exp": now + 300,
    }
    token = jwt.encode(claims, secret, algorithm="HS256", headers={"typ": "JWT"})

    principal = verify_local_session_legacy_v1(token)

    assert principal is not None
    assert principal.profile_id == "local-session-legacy-v1"
    assert principal.subject == claims["sub"]
    assert principal.claims["username"] == "alice"

    unsigned = jwt.encode(claims, key="", algorithm="none", headers={"typ": "JWT"})
    assert verify_local_session_legacy_v1(unsigned) is None
    assert verify_local_session_legacy_v1(_replace_header(token, alg="RS256")) is None
    assert verify_local_session_legacy_v1(_replace_header(token, typ="AKB-APP")) is None

    for required in ("sub", "username", "iat", "exp"):
        incomplete = dict(claims)
        incomplete.pop(required)
        encoded = jwt.encode(
            incomplete,
            secret,
            algorithm="HS256",
            headers={"typ": "JWT"},
        )
        assert verify_local_session_legacy_v1(encoded) is None


@pytest.mark.asyncio
async def test_keycloak_access_v1_accepts_exact_api_profile(monkeypatch, rsa_keypair):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
    )

    principal = await service.verify_access_token(
        token,
        api_audience,
        route_profile="api",
    )

    assert principal is not None
    assert principal.profile_id == "keycloak-access-v1"
    assert (principal.issuer, principal.subject) == (issuer, "human-subject-1")
    assert principal.audience == api_audience
    assert "akb:vault:read" in principal.claims["scope"]


@pytest.mark.asyncio
async def test_keycloak_access_v1_accepts_realistic_multi_audience_user_token(
    monkeypatch,
    rsa_keypair,
):
    """Keycloak may retain built-in audiences beside the selected resource."""
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=["account", api_audience],
    )

    principal = await service.verify_access_token(
        token,
        api_audience,
        route_profile="api",
    )

    assert principal is not None
    assert principal.audience == api_audience


@pytest.mark.asyncio
async def test_keycloak_api_profile_binds_azp_to_configured_human_clients(
    monkeypatch,
    rsa_keypair,
):
    from app.services import auth_service, keycloak_oidc

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = keycloak_oidc.KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    monkeypatch.setattr(keycloak_oidc, "get_keycloak_oidc", lambda: service)

    projected: list[str] = []

    async def forbidden_projection(principal):
        projected.append(principal.subject)
        raise AssertionError("wrong API azp must be rejected before projection")

    monkeypatch.setattr(auth_service, "project_verified_principal", forbidden_projection)
    wrong_azp = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
        claim_overrides={"azp": "untrusted-client"},
    )

    assert await auth_service.resolve_rest_user_authorization(f"Bearer {wrong_azp}") is None
    assert projected == []

    companion_token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
        claim_overrides={"azp": "companion-client"},
    )
    assert (
        await service.verify_access_token(
            companion_token,
            api_audience,
            route_profile="api",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_keycloak_mcp_profile_does_not_apply_static_human_azp_allowlist(
    monkeypatch,
    rsa_keypair,
):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, _, mcp_audience = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    dcr_token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=mcp_audience,
        claim_overrides={"azp": "dynamically-registered-mcp-client"},
    )

    assert (
        await service.verify_access_token(
            dcr_token,
            mcp_audience,
            route_profile="mcp",
        )
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://attacker.example/realms/akb"),
        ("aud", "https://other.example/api"),
        ("typ", "ID"),
        ("sub", _MISSING),
        ("iat", _MISSING),
        ("exp", _MISSING),
        ("jti", _MISSING),
        ("azp", _MISSING),
        ("sid", _MISSING),
        ("scope", _MISSING),
        ("azp", ""),
        ("sid", ""),
        ("scope", ""),
        ("email", ["alice@example.com"]),
        ("email_verified", "true"),
        ("name", {"display": "Alice"}),
        ("preferred_username", ["alice"]),
    ],
)
async def test_keycloak_access_v1_rejects_wrong_or_incomplete_profile_claims(
    monkeypatch,
    rsa_keypair,
    claim: str,
    value: object,
):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
        claim_overrides={claim: value},
    )

    assert await service.verify_access_token(token, api_audience, route_profile="api") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("jku", "https://attacker.example/jwks.json"),
        ("x5u", "https://attacker.example/cert.pem"),
        ("jwk", {"kty": "oct", "k": "attacker"}),
        ("x5c", ["attacker-certificate"]),
    ],
)
async def test_keycloak_access_v1_rejects_token_supplied_key_sources_before_jwks(
    monkeypatch,
    rsa_keypair,
    header: str,
    value: object,
):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()

    async def forbidden_fetch(*, force: bool = False):
        raise AssertionError(f"token header must be rejected before JWKS fetch: {force}")

    service._fetch_jwks = forbidden_fetch  # type: ignore[method-assign]
    token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
        header_overrides={header: value},
    )
    assert await service.verify_access_token(token, api_audience, route_profile="api") is None


@pytest.mark.asyncio
async def test_keycloak_access_v1_rejects_none_wrong_alg_and_wrong_header_type(
    monkeypatch,
    rsa_keypair,
):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
    )
    claims = _keycloak_claims(issuer=issuer, audience=api_audience)
    unsigned = jwt.encode(
        claims,
        key="",
        algorithm="none",
        headers={"kid": "profile-key-1", "typ": "JWT"},
    )

    assert await service.verify_access_token(unsigned, api_audience, route_profile="api") is None
    assert (
        await service.verify_access_token(
            _replace_header(token, alg="HS256"),
            api_audience,
            route_profile="api",
        )
        is None
    )
    assert (
        await service.verify_access_token(
            _replace_header(token, typ="JOSE"),
            api_audience,
            route_profile="api",
        )
        is None
    )


@pytest.mark.asyncio
async def test_keycloak_access_v1_unknown_kid_refreshes_same_pinned_jwks_once(
    monkeypatch,
    rsa_keypair,
):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": []}
    calls: list[bool] = []
    requested_urls: list[str] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"keys": []}

    class Client:
        async def get(self, url: str):
            requested_urls.append(url)
            return Response()

    original_fetch = service._fetch_jwks

    async def fetch(*, force: bool = False):
        calls.append(force)
        return await original_fetch(force=force)

    service._fetch_jwks = fetch  # type: ignore[method-assign]
    service._client = lambda: Client()  # type: ignore[method-assign]
    token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
        header_overrides={"kid": "unknown-key"},
    )

    assert await service.verify_access_token(token, api_audience, route_profile="api") is None
    assert calls == [False, True]
    assert requested_urls == [settings.keycloak_jwks_uri]


@pytest.mark.asyncio
async def test_keycloak_access_v1_unknown_kid_refresh_is_single_flight_and_bounded(
    monkeypatch,
    rsa_keypair,
):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": []}
    requested_urls: list[str] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"keys": []}

    class Client:
        async def get(self, url: str):
            requested_urls.append(url)
            await asyncio.sleep(0)
            return Response()

    service._client = lambda: Client()  # type: ignore[method-assign]
    tokens = [
        _mint_keycloak_token(
            rsa_keypair,
            issuer=issuer,
            audience=api_audience,
            header_overrides={"kid": f"attacker-key-{index}"},
        )
        for index in range(32)
    ]

    results = await asyncio.gather(
        *(service.verify_access_token(token, api_audience, route_profile="api") for token in tokens)
    )

    assert results == [None] * len(tokens)
    assert requested_urls == [settings.keycloak_jwks_uri]


@pytest.mark.asyncio
async def test_keycloak_access_v1_rejects_id_token_service_account_and_wrong_route(
    monkeypatch,
    rsa_keypair,
):
    from app.services.keycloak_oidc import KeycloakOIDC

    issuer, api_audience, mcp_audience = _configure_keycloak(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    id_token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
        claim_overrides={"typ": "ID"},
    )
    service_account = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
        claim_overrides={
            "client_id": "machine-client",
            "preferred_username": "service-account-machine-client",
            "sid": "",
        },
    )
    api_token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
    )
    mcp_token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=mcp_audience,
    )
    cross_route_token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=[api_audience, mcp_audience],
    )

    assert await service.verify_access_token(id_token, api_audience, route_profile="api") is None
    assert (
        await service.verify_access_token(
            service_account,
            api_audience,
            route_profile="api",
        )
        is None
    )
    assert await service.verify_access_token(api_token, mcp_audience, route_profile="mcp") is None
    assert await service.verify_access_token(mcp_token, api_audience, route_profile="api") is None
    assert (
        await service.verify_access_token(
            cross_route_token,
            api_audience,
            route_profile="api",
        )
        is None
    )
    assert (
        await service.verify_access_token(
            cross_route_token,
            mcp_audience,
            route_profile="mcp",
        )
        is None
    )


@pytest.mark.asyncio
async def test_invalid_keycloak_profile_never_reaches_account_projection(
    monkeypatch,
    rsa_keypair,
):
    from app.services import auth_service, keycloak_oidc

    issuer, api_audience, _ = _configure_keycloak(monkeypatch)
    service = keycloak_oidc.KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    token = _mint_keycloak_token(
        rsa_keypair,
        issuer=issuer,
        audience=api_audience,
        claim_overrides={"typ": "ID"},
    )
    monkeypatch.setattr(keycloak_oidc, "get_keycloak_oidc", lambda: service)

    async def forbidden_projection(_claims):
        raise AssertionError("invalid profile must be rejected before account projection")

    monkeypatch.setattr(
        auth_service,
        "_resolve_or_provision_keycloak_user",
        forbidden_projection,
    )
    assert await auth_service.resolve_rest_user_authorization(f"Bearer {token}") is None
