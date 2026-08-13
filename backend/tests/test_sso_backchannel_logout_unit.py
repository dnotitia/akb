"""Fixed verifier profile for Keycloak/OIDC back-channel logout tokens."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest

from app.config import settings
from app.exceptions import AKBError, AuthenticationError
from app.services.keycloak_oidc import KeycloakOIDC


_EVENT = "http://schemas.openid.net/event/backchannel-logout"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@pytest.fixture
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    numbers = private.public_key().public_numbers()

    def number(value: int) -> str:
        return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))

    return {
        "private": private_pem,
        "jwk": {
            "kty": "RSA",
            "kid": "logout-key-1",
            "use": "sig",
            "alg": "RS256",
            "n": number(numbers.n),
            "e": number(numbers.e),
        },
    }


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_server_url", "https://id.example", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web", raising=False)


def _mint(keypair, **overrides: object) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    claims: dict[str, object] = {
        "iss": "https://id.example/realms/akb",
        "aud": "akb-web",
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
        "sid": "keycloak-session-1",
        "sub": "subject-1",
        "typ": "Logout",
        "events": {_EVENT: {}},
    }
    for key, value in overrides.items():
        if value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    return jwt.encode(
        claims,
        keypair["private"],
        algorithm="RS256",
        headers={"kid": "logout-key-1", "typ": "JWT"},
    )


@pytest.mark.asyncio
async def test_keycloak_logout_profile_accepts_exact_signed_sid_selector(
    monkeypatch,
    keypair,
):
    _configure(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [keypair["jwk"]]}  # noqa: SLF001

    claims = await service.verify_backchannel_logout_token(_mint(keypair))

    assert claims["sid"] == "keycloak-session-1"
    assert claims["sub"] == "subject-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://evil.example/realms/akb"},
        {"aud": "another-client"},
        {"sid": None},
        {"typ": "Bearer"},
        {"events": {"another-event": {}}},
        {"nonce": "prohibited"},
        {"iat": 1, "exp": 2},
    ],
)
async def test_logout_profile_rejects_cross_jwt_and_unbounded_tokens(
    monkeypatch,
    keypair,
    changes,
):
    _configure(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [keypair["jwk"]]}  # noqa: SLF001

    with pytest.raises(AuthenticationError, match="Invalid back-channel"):
        await service.verify_backchannel_logout_token(_mint(keypair, **changes))


@pytest.mark.asyncio
async def test_logout_profile_rejects_token_supplied_key_source(monkeypatch, keypair):
    _configure(monkeypatch)
    service = KeycloakOIDC()
    service._jwks = {"keys": [keypair["jwk"]]}  # noqa: SLF001
    token = _mint(keypair)
    header, payload, signature = token.split(".")
    decoded = jwt.get_unverified_header(token)
    decoded["jku"] = "https://evil.example/jwks"
    encoded = _b64url(__import__("json").dumps(decoded, separators=(",", ":")).encode())

    with pytest.raises(AuthenticationError, match="Invalid back-channel"):
        await service.verify_backchannel_logout_token(".".join((encoded, payload, signature)))


@pytest.mark.asyncio
async def test_logout_profile_preserves_pinned_jwks_availability_failure(
    monkeypatch,
    keypair,
):
    _configure(monkeypatch)
    service = KeycloakOIDC()

    async def unavailable(*, force: bool = False):
        del force
        raise AKBError("Pinned JWKS unavailable", status_code=502)

    service._fetch_jwks = unavailable  # type: ignore[method-assign]

    with pytest.raises(AKBError) as captured:
        await service.verify_backchannel_logout_token(_mint(keypair))

    assert captured.value.status_code == 502
