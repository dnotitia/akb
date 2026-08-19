"""Versioned verifier profiles selected by trusted route capabilities."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

import jwt

from app.config import AuthModeConfigurationError, settings

LOCAL_SESSION_RS256_V2 = "local-session-rs256-v2"
KEYCLOAK_ACCESS_V1 = "keycloak-access-v1"
KEYCLOAK_SERVICE_AUTHORITY_V1 = "keycloak-service-authority-v1"

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


def _strict_jose_header(
    token: str,
    *,
    algorithm: str,
    jose_type: str = "JWT",
    require_kid: bool = False,
) -> Mapping[str, Any] | None:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    if header.get("alg") != algorithm or header.get("typ") != jose_type:
        return None
    if _TOKEN_SUPPLIED_KEY_HEADERS.intersection(header):
        return None
    if require_kid:
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 128:
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


def verify_local_session_rs256_v2(token: str) -> VerifiedPrincipal | None:
    """Verify the active local human-session profile with no legacy fallback."""
    from app.services.local_session_keys import (
        LOCAL_SESSION_JOSE_TYPE,
        LocalSessionKeyConfigurationError,
        get_local_session_keyset,
    )

    header = _strict_jose_header(
        token,
        algorithm="RS256",
        jose_type=LOCAL_SESSION_JOSE_TYPE,
        require_kid=True,
    )
    if header is None:
        return None
    kid = header["kid"]
    try:
        keyset = get_local_session_keyset()
    except LocalSessionKeyConfigurationError:
        return None
    public_key = keyset.public_keys.get(kid)
    if public_key is None:
        return None
    issuer = settings.local_session_issuer_effective
    audience = settings.local_session_audience_effective
    if not issuer or not audience:
        return None
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={
                "require": [
                    "iss",
                    "aud",
                    "sub",
                    "username",
                    "iat",
                    "nbf",
                    "exp",
                    "jti",
                    "profile",
                    "token_use",
                ]
            },
        )
    except jwt.PyJWTError:
        return None

    subject = _required_nonempty_string(claims, "sub")
    username = _required_nonempty_string(claims, "username")
    jti = _required_nonempty_string(claims, "jti")
    issued_at = _required_numeric_date(claims, "iat")
    not_before = _required_numeric_date(claims, "nbf")
    expires_at = _required_numeric_date(claims, "exp")
    if (
        subject is None
        or username is None
        or jti is None
        or issued_at is None
        or not_before is None
        or expires_at is None
        or expires_at <= issued_at
        or not_before < issued_at
        or claims.get("profile") != LOCAL_SESSION_RS256_V2
        or claims.get("token_use") != "session"
    ):
        return None
    try:
        uuid.UUID(subject)
        uuid.UUID(jti)
    except ValueError, AttributeError:
        return None

    return VerifiedPrincipal(
        profile_id=LOCAL_SESSION_RS256_V2,
        issuer=issuer,
        subject=subject,
        credential_type="session",
        claims=claims,
        audience=audience,
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


async def verify_keycloak_service_authority_v1(token: str) -> VerifiedPrincipal | None:
    """Verify the one configured non-human administrative credential.

    Deliberately not a route profile: there is exactly one authority and one
    route class (REST) that may present it. It carries no route audience, so
    there is nothing for a route selector to choose between, and adding one
    would only create a second way to reach the same principal.
    """
    if not settings.keycloak_enabled:
        return None
    if not settings.keycloak_service_admin_client_id_effective:
        return None
    try:
        if not settings.sso_human_auth_enabled:
            # The capability lives on the human REST surface. `local` mode
            # resolves that surface with the local-session profile only.
            return None
    except AuthModeConfigurationError:
        return None

    from app.services.keycloak_oidc import get_keycloak_oidc

    return await get_keycloak_oidc().verify_service_authority_token(token)
