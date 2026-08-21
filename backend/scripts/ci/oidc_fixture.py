"""Small, deterministic OIDC Resource Server fixture for the E2E runtime.

The fixture deliberately implements only the authority surface that AKB's
Resource Server verifier consumes: an ephemeral RSA key, the realm JWKS
endpoint, discovery metadata, and bounded token variants.  It does not model
authorization, browser login, consent, or a Keycloak-compatible server.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


OIDC_TOKEN_VARIANTS: tuple[str, ...] = (
    "valid",
    "wrong_issuer",
    "wrong_audience",
    "expired",
    "wrong_algorithm",
    "wrong_key_id",
    "insufficient_scope",
)


def _base64url(value: int) -> str:
    width = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(width, "big")).rstrip(b"=").decode("ascii")


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: str = Field(min_length=1, max_length=64)


@dataclass(slots=True)
class OIDCFixture:
    """In-process OIDC fixture bound to one runtime origin and candidate."""

    origin: str
    realm: str
    audience: str
    subject: str = "runtime-oidc-subject"
    username: str = "runtime-oidc-user"
    email: str = "runtime-oidc-user@example.invalid"
    _private_key: Any = field(init=False, repr=False)
    _key_id: str = field(init=False, repr=False)
    _jwk: dict[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._key_id = f"runtime-{uuid.uuid4().hex[:12]}"
        self._jwk = self._make_jwk()

    @property
    def issuer(self) -> str:
        return f"{self.origin.rstrip('/')}/realms/{self.realm}"

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/certs"

    @property
    def token_uri(self) -> str:
        return f"{self.origin.rstrip('/')}/oidc/token"

    @property
    def metadata_uri(self) -> str:
        return f"{self.origin.rstrip('/')}/.well-known/openid-configuration"

    @property
    def health_uri(self) -> str:
        return f"{self.origin.rstrip('/')}/oidc/health"

    def _make_jwk(self) -> dict[str, str]:
        public = self._private_key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self._key_id,
            "n": _base64url(public.n),
            "e": _base64url(public.e),
        }

    def discovery(self) -> dict[str, object]:
        """Return only coordinates and variant names, never token material."""

        return {
            "issuer": self.issuer,
            "jwks": {"method": "GET", "url": self.jwks_uri},
            "token_variants": list(OIDC_TOKEN_VARIANTS),
            "token": {"method": "POST", "url": self.token_uri, "body": {"variant": "valid"}},
            "metadata": {"method": "GET", "url": self.metadata_uri},
            "health": {"method": "GET", "url": self.health_uri},
            "scope_cases": {
                "read": "akb:vault:read",
                "write": "akb:vault:write",
            },
            # These are verifier-boundary cases, not Authorization Server
            # flows.  The common fixture intentionally exposes coordinates so
            # a source-blind consumer can reproduce an absent/malformed
            # bearer and a scope rejection without learning any secret.
            "challenge_cases": {
                "metadata": {
                    "method": "GET",
                    "url": self.metadata_uri,
                },
                "missing_authorization": {"authorization": "omitted"},
                "malformed_bearer": {"authorization": "Bearer <malformed>"},
                "insufficient_scope": {"token_variant": "insufficient_scope"},
            },
        }

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        return {"keys": [dict(self._jwk)]}

    def metadata(self) -> dict[str, object]:
        return {
            "issuer": self.issuer,
            "jwks_uri": self.jwks_uri,
            "token_endpoint": self.token_uri,
            "response_types_supported": [],
            "grant_types_supported": [],
            "authorization_endpoint": None,
            "scopes_supported": ["akb:vault:read", "akb:vault:write"],
        }

    def _claims(self, variant: str) -> dict[str, object]:
        now = int(time.time())
        claims: dict[str, object] = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": self.subject,
            "iat": now,
            "exp": now + 300,
            "jti": hashlib.sha256(f"{self.subject}:{variant}:{now // 300}".encode()).hexdigest(),
            "typ": "Bearer",
            "azp": "runtime-mcp-client",
            "sid": "runtime-mcp-session",
            "scope": "akb:vault:read akb:vault:write",
            "preferred_username": self.username,
            "email": self.email,
            "email_verified": True,
        }
        if variant == "wrong_issuer":
            claims["iss"] = f"{self.origin.rstrip('/')}/realms/other"
        elif variant == "wrong_audience":
            claims["aud"] = f"{self.origin.rstrip('/')}/wrong-audience"
        elif variant == "expired":
            claims["exp"] = now - 10
        elif variant == "insufficient_scope":
            claims["scope"] = "akb:vault:read"
        return claims

    def mint(self, variant: str) -> str:
        if variant not in OIDC_TOKEN_VARIANTS:
            raise ValueError(f"unknown OIDC token variant: {variant}")
        claims = self._claims(variant)
        if variant == "wrong_algorithm":
            # The verifier rejects this before consulting the JWKS.  The
            # signing secret is fixture-local and is never returned.
            return jwt.encode(
                claims,
                "runtime-oidc-invalid-algorithm-key-material-0123456789abcdef",  # pragma: allowlist secret
                algorithm="HS256",
            )
        headers = {"kid": self._key_id, "typ": "JWT", "alg": "RS256"}
        if variant == "wrong_key_id":
            headers["kid"] = "runtime-unknown-key"
        return jwt.encode(claims, self._private_key, algorithm="RS256", headers=headers)

    def register(self, app: FastAPI) -> None:
        """Mount the fixture endpoints on the runtime's existing control app."""

        @app.get("/oidc/health", include_in_schema=True)
        async def oidc_health() -> dict[str, object]:
            return {"status": "ready", "issuer": self.issuer, "jwks_key_count": 1}

        @app.get("/.well-known/openid-configuration", include_in_schema=True)
        async def oidc_metadata() -> dict[str, object]:
            return self.metadata()

        @app.get(f"/realms/{self.realm}/protocol/openid-connect/certs", include_in_schema=True)
        async def oidc_jwks() -> dict[str, list[dict[str, str]]]:
            return self.jwks()

        @app.post("/oidc/token", include_in_schema=True)
        async def oidc_token(request: TokenRequest) -> dict[str, object]:
            try:
                token = self.mint(request.variant)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="unknown OIDC token variant") from exc
            return {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": self._claims(request.variant)["scope"],
            }


__all__ = ["OIDCFixture", "OIDC_TOKEN_VARIANTS"]
