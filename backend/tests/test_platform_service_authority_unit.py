"""The non-human service-authority profile, and the human profile it cannot enter.

The token fixtures here are not invented. They are the exact claim set a
Keycloak realm emits for ``grant_type=client_credentials`` on the client this
project's own SSO bootstrap creates (``serviceAccountsEnabled: true``,
``fullScopeAllowed: false``, ``defaultClientScopes: ["service_account"]``,
no protocol mappers) — measured against Keycloak 26.0 and 26.4:

    iss, sub, iat, exp, jti, typ, azp, scope, client_id, clientHost, clientAddress

with **no** ``aud``, **no** ``sid``, **no** ``preferred_username``, and an empty
``scope`` string. That shape is why this is a separate profile rather than a
relaxation of ``keycloak-access-v1``: the human profile requires ``aud``,
``sid`` and a non-empty ``scope``, and must keep requiring them.
"""

from __future__ import annotations

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


ISSUER = "https://auth-workspace.example.com/realms/akb"
API_AUDIENCE = "https://akb-workspace.example.com/api"
MCP_AUDIENCE = "https://akb-workspace.example.com/mcp"
SERVICE_ADMIN_CLIENT_ID = "akb-sso-manager"
SERVICE_ACCOUNT_SUBJECT = "a9cf6dcd-46ec-42b6-94f4-b6f0312ec15f"

_MISSING = object()


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
    numbers = private.public_key().public_numbers()

    def encode_number(value: int) -> str:
        return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))

    return {
        "private_pem": private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        "jwk": {
            "kty": "RSA",
            "kid": "service-authority-key-1",
            "use": "sig",
            "alg": "RS256",
            "n": encode_number(numbers.n),
            "e": encode_number(numbers.e),
        },
    }


def _configure_workspace(monkeypatch, *, service_admin_client_id: str = SERVICE_ADMIN_CLIENT_ID) -> None:
    """The managed profile an operator renders for one workspace."""
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://auth-workspace.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-workspace-web", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-workspace-admin", raising=False)
    monkeypatch.setattr(settings, "keycloak_management_client_id", SERVICE_ADMIN_CLIENT_ID, raising=False)
    monkeypatch.setattr(settings, "keycloak_service_admin_client_id", service_admin_client_id, raising=False)
    monkeypatch.setattr(settings, "keycloak_companion_client_ids_by_origin", {}, raising=False)
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)
    monkeypatch.setattr(settings, "keycloak_require_verified_email", True, raising=False)
    monkeypatch.setattr(settings, "api_oauth_audience", API_AUDIENCE, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_audience", MCP_AUDIENCE, raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb-workspace.example.com", raising=False)


def _service_authority_claims(*, client_id: str = SERVICE_ADMIN_CLIENT_ID) -> dict[str, object]:
    """The measured client-credentials claim set — nothing added, nothing dropped."""
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "iss": ISSUER,
        "sub": SERVICE_ACCOUNT_SUBJECT,
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
        "typ": "Bearer",
        "azp": client_id,
        "scope": "",
        "client_id": client_id,
        "clientHost": "10.42.0.11",
        "clientAddress": "10.42.0.11",
    }


def _mint(
    rsa_keypair: dict[str, Any],
    claims: dict[str, object],
    *,
    overrides: dict[str, object] | None = None,
    header_overrides: dict[str, object] | None = None,
    key_pem: bytes | None = None,
) -> str:
    payload = dict(claims)
    for name, value in (overrides or {}).items():
        if value is _MISSING:
            payload.pop(name, None)
        else:
            payload[name] = value
    headers: dict[str, object] = {"kid": "service-authority-key-1", "typ": "JWT"}
    headers.update(header_overrides or {})
    return jwt.encode(
        payload,
        key_pem or rsa_keypair["private_pem"],
        algorithm=str(headers.get("alg", "RS256")),
        headers=headers,
    )


def _service_authority_token(
    rsa_keypair: dict[str, Any],
    *,
    client_id: str = SERVICE_ADMIN_CLIENT_ID,
    overrides: dict[str, object] | None = None,
    header_overrides: dict[str, object] | None = None,
) -> str:
    return _mint(
        rsa_keypair,
        _service_authority_claims(client_id=client_id),
        overrides=overrides,
        header_overrides=header_overrides,
    )


def _human_claims() -> dict[str, object]:
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "iss": ISSUER,
        "aud": API_AUDIENCE,
        "sub": "3d0d1a2b-0000-4000-8000-1111c0ffee00",
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "typ": "Bearer",
        "azp": "akb-workspace-web",
        "sid": str(uuid.uuid4()),
        "scope": "openid profile email",
        "preferred_username": "alice",
        "email": "alice@example.com",
        "email_verified": True,
        "name": "Alice",
    }


def _verifier(rsa_keypair: dict[str, Any]):
    from app.services.keycloak_oidc import KeycloakOIDC

    service = KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}

    def forbidden_client():
        raise AssertionError("the pinned JWKS is already cached; no network call is expected")

    service._client = forbidden_client  # type: ignore[method-assign]
    return service


# ── The capability itself ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_measured_management_client_token_verifies_as_service_authority(
    monkeypatch,
    rsa_keypair,
):
    from app.services.auth_verifier_profiles import KEYCLOAK_SERVICE_AUTHORITY_V1

    _configure_workspace(monkeypatch)
    principal = await _verifier(rsa_keypair).verify_service_authority_token(
        _service_authority_token(rsa_keypair)
    )

    assert principal is not None
    assert principal.profile_id == KEYCLOAK_SERVICE_AUTHORITY_V1
    assert principal.issuer == ISSUER
    assert principal.subject == SERVICE_ACCOUNT_SUBJECT
    assert principal.credential_type == "access_token"
    # The measured token carries no `aud`; the profile must not invent one.
    assert principal.audience is None


@pytest.mark.asyncio
async def test_the_capability_is_inert_until_the_exact_client_is_named(monkeypatch, rsa_keypair):
    """Default configuration admits no service-account token at all."""
    _configure_workspace(monkeypatch, service_admin_client_id="")
    assert settings.keycloak_service_admin_client_id_effective == ""

    assert (
        await _verifier(rsa_keypair).verify_service_authority_token(
            _service_authority_token(rsa_keypair)
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_service_account_token_from_a_different_client_is_refused(monkeypatch, rsa_keypair):
    """The boundary: this is one named authority, not "service accounts"."""
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    for other in ("akb-workspace-web", "akb-workspace-admin", "some-other-service", "akb-sso-manager-2"):
        token = _service_authority_token(rsa_keypair, client_id=other)
        assert await service.verify_service_authority_token(token) is None, other

    # ... and a token that merely *claims* the right `client_id` while being
    # issued to another party is refused too.
    mismatched = _service_authority_token(
        rsa_keypair,
        client_id="some-other-service",
        overrides={"client_id": SERVICE_ADMIN_CLIENT_ID},
    )
    assert await service.verify_service_authority_token(mismatched) is None


@pytest.mark.asyncio
async def test_the_service_account_marker_must_be_present_and_agree(monkeypatch, rsa_keypair):
    """A bearer without Keycloak's own machine markers is not this credential."""
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    for overrides in (
        {"client_id": _MISSING},
        {"client_id": "akb-workspace-web"},
        {"preferred_username": "alice"},
        {"preferred_username": "service-account-akb-workspace-web"},
    ):
        token = _service_authority_token(rsa_keypair, overrides=overrides)
        assert await service.verify_service_authority_token(token) is None, overrides

    # Keycloak emits `service-account-<client>` whenever the `profile` scope is
    # attached; the exact form stays acceptable.
    accepted = _service_authority_token(
        rsa_keypair,
        overrides={"preferred_username": f"service-account-{SERVICE_ADMIN_CLIENT_ID}"},
    )
    assert await service.verify_service_authority_token(accepted) is not None


@pytest.mark.asyncio
async def test_a_token_carrying_human_profile_claims_is_not_this_credential(monkeypatch, rsa_keypair):
    """A machine principal has no person behind it. Claims that describe one
    mean the token came from some other flow on this client."""
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    for claim, value in (
        ("email", "someone@example.com"),
        ("email_verified", True),
        ("name", "Someone"),
    ):
        token = _service_authority_token(rsa_keypair, overrides={claim: value})
        assert await service.verify_service_authority_token(token) is None, claim


@pytest.mark.asyncio
async def test_the_pinned_signature_and_issuer_profile_is_unchanged(monkeypatch, rsa_keypair):
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    assert (
        await service.verify_service_authority_token(
            _mint(rsa_keypair, _service_authority_claims(), key_pem=foreign)
        )
        is None
    )

    # Another realm's identically shaped token.
    assert (
        await service.verify_service_authority_token(
            _service_authority_token(
                rsa_keypair,
                overrides={"iss": "https://auth-other.example.com/realms/akb"},
            )
        )
        is None
    )

    # Algorithm and JOSE-type pinning, and token-supplied key material.
    assert (
        await service.verify_service_authority_token(
            jwt.encode(_service_authority_claims(), "secret", algorithm="HS256", headers={"kid": "x"})
        )
        is None
    )
    assert (
        await service.verify_service_authority_token(
            _service_authority_token(rsa_keypair, header_overrides={"typ": "at+jwt"})
        )
        is None
    )
    for header in ("jku", "x5u", "jwk", "x5c"):
        token = _replace_header(_service_authority_token(rsa_keypair), **{header: "https://evil.example.com/keys"})
        assert await service.verify_service_authority_token(token) is None, header


@pytest.mark.asyncio
async def test_an_unresolvable_kid_is_an_availability_failure_not_an_acceptance(
    monkeypatch,
    rsa_keypair,
):
    """Same distinction the human profile makes: a key that cannot be resolved
    is a bounded 502, never a verified principal."""
    import time

    from app.exceptions import AKBError

    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)
    service._jwks_refresh_attempt_at = time.monotonic()

    with pytest.raises(AKBError):
        await service.verify_service_authority_token(
            _service_authority_token(rsa_keypair, header_overrides={"kid": "unknown-kid"})
        )


@pytest.mark.asyncio
async def test_required_claim_shape(monkeypatch, rsa_keypair):
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    for overrides in (
        {"typ": "Refresh"},
        {"typ": _MISSING},
        {"sub": _MISSING},
        {"sub": "   "},
        {"jti": _MISSING},
        {"azp": _MISSING},
        {"iat": _MISSING},
        {"exp": _MISSING},
        {"scope": 7},
    ):
        token = _service_authority_token(rsa_keypair, overrides=overrides)
        assert await service.verify_service_authority_token(token) is None, overrides

    now = int(datetime.now(timezone.utc).timestamp())
    expired = _service_authority_token(rsa_keypair, overrides={"iat": now - 600, "exp": now - 300})
    assert await service.verify_service_authority_token(expired) is None

    inverted = _service_authority_token(rsa_keypair, overrides={"iat": now + 300, "exp": now + 60})
    assert await service.verify_service_authority_token(inverted) is None


@pytest.mark.asyncio
async def test_audience_is_optional_but_another_route_audience_is_refused(monkeypatch, rsa_keypair):
    """The measured token has no `aud` and this profile does not require one:
    (issuer, azp) already names exactly one client in exactly one realm, and
    Keycloak cannot add an audience to a client-credentials token without an
    operator adding a mapper. What it must never be is another route's token."""
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    assert await service.verify_service_authority_token(_service_authority_token(rsa_keypair)) is not None

    # An operator who does add an audience mapper stays compatible.
    with_audience = _service_authority_token(rsa_keypair, overrides={"aud": API_AUDIENCE})
    assert await service.verify_service_authority_token(with_audience) is not None

    for audience in (MCP_AUDIENCE, [API_AUDIENCE, MCP_AUDIENCE]):
        token = _service_authority_token(rsa_keypair, overrides={"aud": audience})
        assert await service.verify_service_authority_token(token) is None, audience


# ── The human profile it must never enter ───────────────────────────


@pytest.mark.asyncio
async def test_the_human_api_profile_still_refuses_the_service_authority_token(
    monkeypatch,
    rsa_keypair,
):
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    assert (
        await service.verify_access_token(
            _service_authority_token(rsa_keypair),
            API_AUDIENCE,
            route_profile="api",
        )
        is None
    )
    # Even shaped to satisfy every human requirement the measured token lacks.
    now = int(datetime.now(timezone.utc).timestamp())
    dressed = _service_authority_token(
        rsa_keypair,
        overrides={
            "aud": API_AUDIENCE,
            "sid": str(uuid.uuid4()),
            "scope": "openid profile email",
            "iat": now,
            "exp": now + 300,
        },
    )
    assert await service.verify_access_token(dressed, API_AUDIENCE, route_profile="api") is None


@pytest.mark.asyncio
async def test_naming_a_client_as_service_authority_never_makes_it_a_human_client(
    monkeypatch,
    rsa_keypair,
):
    """Misconfiguring the same client into the human allowlist must not open
    the human route to it."""
    _configure_workspace(monkeypatch)
    monkeypatch.setattr(
        settings,
        "keycloak_companion_client_ids_by_origin",
        {"https://platform.example.com": SERVICE_ADMIN_CLIENT_ID},
        raising=False,
    )
    assert SERVICE_ADMIN_CLIENT_ID in settings.keycloak_human_client_ids
    service = _verifier(rsa_keypair)

    now = int(datetime.now(timezone.utc).timestamp())
    human_shaped = _service_authority_token(
        rsa_keypair,
        overrides={
            "aud": API_AUDIENCE,
            "sid": str(uuid.uuid4()),
            "scope": "openid profile email",
            "preferred_username": "not-a-service-account",
            "client_id": _MISSING,
            "clientHost": _MISSING,
            "clientAddress": _MISSING,
            "iat": now,
            "exp": now + 300,
        },
    )
    assert await service.verify_access_token(human_shaped, API_AUDIENCE, route_profile="api") is None


@pytest.mark.asyncio
async def test_a_colliding_configuration_makes_the_capability_inert_at_runtime_too(
    monkeypatch,
    rsa_keypair,
):
    """The canonical loader refuses the collision. A Settings object assembled
    around it must still fail closed rather than admit the machine."""
    _configure_workspace(monkeypatch)
    monkeypatch.setattr(
        settings,
        "keycloak_companion_client_ids_by_origin",
        {"https://platform.example.com": SERVICE_ADMIN_CLIENT_ID},
        raising=False,
    )
    assert settings.keycloak_service_admin_client_id_effective == ""

    assert (
        await _verifier(rsa_keypair).verify_service_authority_token(
            _service_authority_token(rsa_keypair)
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_human_token_is_refused_by_the_service_authority_profile(monkeypatch, rsa_keypair):
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    assert await service.verify_service_authority_token(_mint(rsa_keypair, _human_claims())) is None
    # Even with the authorized party forced to the named client.
    forced = _mint(rsa_keypair, _human_claims(), overrides={"azp": SERVICE_ADMIN_CLIENT_ID})
    assert await service.verify_service_authority_token(forced) is None


@pytest.mark.asyncio
async def test_service_authority_is_not_reachable_on_the_mcp_route(monkeypatch, rsa_keypair):
    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)

    now = int(datetime.now(timezone.utc).timestamp())
    mcp_shaped = _service_authority_token(
        rsa_keypair,
        overrides={
            "aud": MCP_AUDIENCE,
            "sid": str(uuid.uuid4()),
            "scope": "akb:vault:read akb:vault:write",
            "iat": now,
            "exp": now + 300,
        },
    )
    assert await service.verify_access_token(mcp_shaped, MCP_AUDIENCE, route_profile="mcp") is None


# ── Selection, projection, and the resolvers that must not admit it ──


@pytest.mark.asyncio
async def test_rest_resolution_selects_the_service_authority_profile(monkeypatch, rsa_keypair):
    from app.services import auth_service
    from app.services.auth_verifier_profiles import KEYCLOAK_SERVICE_AUTHORITY_V1

    _configure_workspace(monkeypatch)
    service = _verifier(rsa_keypair)
    token = _service_authority_token(rsa_keypair)

    async def forbidden_human(*_args, **_kwargs):
        raise AssertionError("a service-authority bearer must not enter the human profile")

    async def verify_service(value: str):
        return await service.verify_service_authority_token(value)

    projected: list[object] = []

    async def project(principal, **_kwargs):
        projected.append(principal)
        return "resolved-service-principal"

    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", forbidden_human)
    monkeypatch.setattr(auth_service, "verify_keycloak_service_authority_v1", verify_service)
    monkeypatch.setattr(auth_service, "project_verified_principal", project)

    resolved = await auth_service.resolve_rest_user_authorization(f"Bearer {token}")

    assert resolved == "resolved-service-principal"
    assert [p.profile_id for p in projected] == [KEYCLOAK_SERVICE_AUTHORITY_V1]


@pytest.mark.asyncio
async def test_a_human_bearer_still_selects_only_the_human_profile(monkeypatch, rsa_keypair):
    from app.services import auth_service

    _configure_workspace(monkeypatch)
    token = _mint(rsa_keypair, _human_claims())

    async def forbidden_service(*_args, **_kwargs):
        raise AssertionError("a human bearer must not enter the service-authority profile")

    seen: list[tuple[str, str]] = []

    async def verify_human(value: str, route_profile: str):
        seen.append((value, route_profile))
        return None

    monkeypatch.setattr(auth_service, "verify_keycloak_service_authority_v1", forbidden_service)
    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", verify_human)

    assert await auth_service.resolve_rest_user_authorization(f"Bearer {token}") is None
    assert [route for _, route in seen] == ["api"]


@pytest.mark.asyncio
async def test_service_authority_is_not_a_delegated_human_and_not_an_mcp_credential(
    monkeypatch,
    rsa_keypair,
):
    from app.services import auth_service

    _configure_workspace(monkeypatch)
    token = _service_authority_token(rsa_keypair)

    async def forbidden_service(*_args, **_kwargs):
        raise AssertionError("only the REST capability may select the service-authority profile")

    async def refuse_human(_token: str, _route_profile: str):
        return None

    monkeypatch.setattr(auth_service, "verify_keycloak_service_authority_v1", forbidden_service)
    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", refuse_human)

    assert await auth_service.resolve_delegated_human_authorization(f"Bearer {token}") is None
    assert await auth_service.resolve_mcp_authorization(f"Bearer {token}") is None


@pytest.mark.asyncio
async def test_projection_never_reaches_the_human_account_path(monkeypatch, rsa_keypair):
    from app.services import auth_service
    from app.services.auth_verifier_profiles import KEYCLOAK_SERVICE_AUTHORITY_V1, VerifiedPrincipal

    _configure_workspace(monkeypatch)

    async def forbidden_human_resolution(_claims):
        raise AssertionError("a machine principal must never enter human enrollment")

    resolved: list[tuple[str, str, str]] = []

    async def fake_resolve_service_authority(*, issuer: str, client_id: str, subject: str):
        resolved.append((issuer, client_id, subject))
        return {
            "user_id": uuid.UUID("11111111-2222-4333-8444-555555555555"),
            "username": f"service-{client_id}",
            "email": f"service-{client_id}@service.invalid",
            "display_name": None,
            "is_admin": True,
            "newly_bound": False,
        }

    monkeypatch.setattr(auth_service, "_resolve_or_provision_keycloak_user", forbidden_human_resolution)
    monkeypatch.setattr(auth_service, "resolve_service_authority", fake_resolve_service_authority)

    principal = VerifiedPrincipal(
        profile_id=KEYCLOAK_SERVICE_AUTHORITY_V1,
        issuer=ISSUER,
        subject=SERVICE_ACCOUNT_SUBJECT,
        credential_type="access_token",
        claims=_service_authority_claims(),
        audience=None,
    )
    user = await auth_service.project_verified_principal(principal)

    assert user is not None
    assert resolved == [(ISSUER, SERVICE_ADMIN_CLIENT_ID, SERVICE_ACCOUNT_SUBJECT)]
    assert user.is_admin is True
    assert user.account_kind == "service"
    assert user.auth_method == "oauth"
    # An empty `scope` string must not become a phantom OAuth scope.
    assert user.oauth_scopes == []
    assert user.token_scopes is None


# ── Configuration ────────────────────────────────────────────────────


def test_the_service_authority_client_may_not_be_a_human_or_admin_client():
    from app.config import AuthModeConfigurationError, Settings

    def build(**overrides):
        values = {
            "auth_mode": "sso",
            "keycloak_enabled": True,
            "keycloak_server_url": "https://auth-workspace.example.com",
            "keycloak_realm": "akb",
            "keycloak_client_id": "akb-workspace-web",
            "keycloak_admin_client_id": "akb-workspace-admin",
            "keycloak_companion_client_ids_by_origin": {"https://companion.example.com": "akb-companion"},
            "public_base_url": "https://akb-workspace.example.com",
        }
        values.update(overrides)
        return Settings(**values)

    assert build().keycloak_service_admin_client_id_effective == ""
    assert (
        build(keycloak_service_admin_client_id=SERVICE_ADMIN_CLIENT_ID).keycloak_service_admin_client_id_effective
        == SERVICE_ADMIN_CLIENT_ID
    )

    for collision in ("akb-workspace-web", "akb-workspace-admin", "akb-companion"):
        with pytest.raises(AuthModeConfigurationError):
            build(keycloak_service_admin_client_id=collision)


def test_a_service_authority_client_requires_a_configured_keycloak_authority():
    from app.config import AuthModeConfigurationError, Settings

    with pytest.raises(AuthModeConfigurationError):
        Settings(
            auth_mode="local",
            keycloak_enabled=False,
            keycloak_service_admin_client_id=SERVICE_ADMIN_CLIENT_ID,
            public_base_url="https://akb-workspace.example.com",
        )


# ── The request boundary the platform actually calls ─────────────────


def _admin_app():
    from fastapi import Depends, FastAPI

    from app.api import deps
    from app.api.routes.access import _require_admin
    from app.services.auth_service import AuthenticatedUser

    app = FastAPI()

    @app.post("/admin/service-users/ensure")
    async def ensure(user: AuthenticatedUser = Depends(deps.get_current_user)):
        _require_admin(user)
        return {
            "user_id": user.user_id,
            "is_admin": user.is_admin,
            "account_kind": user.account_kind,
            "auth_method": user.auth_method,
        }

    return app


def _install_verifier(monkeypatch, rsa_keypair):
    from app.services import keycloak_oidc

    service = _verifier(rsa_keypair)
    monkeypatch.setattr(keycloak_oidc, "get_keycloak_oidc", lambda: service)
    return service


def _install_binding(monkeypatch, *, is_admin: bool = True):
    from app.services import auth_service

    async def fake_resolve_service_authority(*, issuer: str, client_id: str, subject: str):
        return {
            "user_id": uuid.UUID("11111111-2222-4333-8444-555555555555"),
            "username": f"service-{client_id}",
            "email": f"service-{client_id}@service.invalid",
            "display_name": None,
            "is_admin": is_admin,
            "newly_bound": False,
        }

    monkeypatch.setattr(auth_service, "resolve_service_authority", fake_resolve_service_authority)


def test_the_measured_bearer_reaches_an_admin_route(monkeypatch, rsa_keypair):
    """The whole boundary: bearer → verifier → binding → scope gate → admin gate."""
    from fastapi.testclient import TestClient

    _configure_workspace(monkeypatch)
    _install_verifier(monkeypatch, rsa_keypair)
    _install_binding(monkeypatch)

    response = TestClient(_admin_app()).post(
        "/admin/service-users/ensure",
        headers={"Authorization": f"Bearer {_service_authority_token(rsa_keypair)}"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "user_id": "11111111-2222-4333-8444-555555555555",
        "is_admin": True,
        "account_kind": "service",
        "auth_method": "oauth",
    }


def test_the_same_bearer_is_401_while_the_capability_is_inert(monkeypatch, rsa_keypair):
    from fastapi.testclient import TestClient

    _configure_workspace(monkeypatch, service_admin_client_id="")
    _install_verifier(monkeypatch, rsa_keypair)
    _install_binding(monkeypatch)

    response = TestClient(_admin_app()).post(
        "/admin/service-users/ensure",
        headers={"Authorization": f"Bearer {_service_authority_token(rsa_keypair)}"},
    )
    assert response.status_code == 401


def test_a_service_account_bearer_from_another_client_is_401(monkeypatch, rsa_keypair):
    from fastapi.testclient import TestClient

    _configure_workspace(monkeypatch)
    _install_verifier(monkeypatch, rsa_keypair)
    _install_binding(monkeypatch)

    response = TestClient(_admin_app()).post(
        "/admin/service-users/ensure",
        headers={
            "Authorization": f"Bearer {_service_authority_token(rsa_keypair, client_id='akb-other-service')}"
        },
    )
    assert response.status_code == 401


def test_an_operator_demotion_reaches_the_route_as_a_refusal_not_an_acceptance(
    monkeypatch,
    rsa_keypair,
):
    from fastapi.testclient import TestClient

    _configure_workspace(monkeypatch)
    _install_verifier(monkeypatch, rsa_keypair)
    _install_binding(monkeypatch, is_admin=False)

    response = TestClient(_admin_app()).post(
        "/admin/service-users/ensure",
        headers={"Authorization": f"Bearer {_service_authority_token(rsa_keypair)}"},
    )
    assert response.status_code == 403
