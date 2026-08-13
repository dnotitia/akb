"""Versioned verifier profiles selected by trusted route capabilities."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

import jwt

from app.config import AuthModeConfigurationError, settings

LOCAL_SESSION_LEGACY_V1 = "local-session-legacy-v1"
KEYCLOAK_ACCESS_V1 = "keycloak-access-v1"
LOCAL_SESSION_ISSUER = "akb-local"

CredentialType = Literal["session", "access_token"]
KeycloakRouteProfile = Literal["api", "mcp"]

_TOKEN_SUPPLIED_KEY_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})


@dataclass(frozen=True, slots=True)
class VerifiedPrincipal:
    """Identity and credential facts established by one selected profile.

    This object deliberately has no AKB account state, grants, or admin role.
    Those belong to the existing account projection and authorization path.
    """

    profile_id: str
    issuer: str
    subject: str
    credential_type: CredentialType
    claims: Mapping[str, Any]
    audience: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "claims", MappingProxyType(dict(self.claims)))


def _strict_jose_header(token: str, *, algorithm: str) -> Mapping[str, Any] | None:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    if header.get("alg") != algorithm or header.get("typ") != "JWT":
        return None
    if _TOKEN_SUPPLIED_KEY_HEADERS.intersection(header):
        return None
    return header


def _required_nonempty_string(claims: Mapping[str, Any], name: str) -> str | None:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _required_numeric_date(claims: Mapping[str, Any], name: str) -> int | None:
    value = claims.get(name)
    if type(value) is not int:
        return None
    return value


def verify_local_session_legacy_v1(token: str) -> VerifiedPrincipal | None:
    """Verify the migration-only local human-session profile.

    Existing local sessions are fixed to HS256 and the installation secret.
    The configurable algorithm field is intentionally not consulted.
    """
    if _strict_jose_header(token, algorithm="HS256") is None:
        return None
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["sub", "username", "iat", "exp"]},
        )
    except jwt.PyJWTError:
        return None

    subject = _required_nonempty_string(claims, "sub")
    username = _required_nonempty_string(claims, "username")
    issued_at = _required_numeric_date(claims, "iat")
    expires_at = _required_numeric_date(claims, "exp")
    if subject is None or username is None or issued_at is None or expires_at is None or expires_at <= issued_at:
        return None
    try:
        uuid.UUID(subject)
    except ValueError, AttributeError:
        return None

    return VerifiedPrincipal(
        profile_id=LOCAL_SESSION_LEGACY_V1,
        issuer=LOCAL_SESSION_ISSUER,
        subject=subject,
        credential_type="session",
        claims=claims,
        audience=None,
    )


async def verify_keycloak_access_v1(
    token: str,
    route_profile: KeycloakRouteProfile,
) -> VerifiedPrincipal | None:
    """Verify the access-token profile selected for one trusted route class."""
    if not settings.keycloak_enabled:
        return None
    if route_profile == "api":
        try:
            if not settings.sso_human_auth_enabled:
                return None
        except AuthModeConfigurationError:
            return None
        audience = settings.api_oauth_audience_effective
    elif route_profile == "mcp":
        if not settings.mcp_oauth_enabled:
            return None
        audience = settings.mcp_oauth_audience_effective
    else:
        return None
    if not audience:
        return None

    from app.services.keycloak_oidc import get_keycloak_oidc

    return await get_keycloak_oidc().verify_access_token(
        token,
        audience,
        route_profile=route_profile,
    )
