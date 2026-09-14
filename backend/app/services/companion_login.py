"""Opt-in BFF account completion. Never used by bearer resolution or refresh.

A deployment RSA assertion attests that the registered BFF performed its own
browser-bound code flow. Keycloak tokens are independently verified here; the
BFF assertion is not a replacement for end-user authentication.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from datetime import datetime, timezone

import jwt
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import (
    AccountSuspendedError, AuthenticationError, ExternalIdentityConflictError, MembershipRequiredError,
)
from app.services.auth_service import project_verified_principal_with_reason
from app.services.keycloak_oidc import get_keycloak_oidc
from app.sso import local_realm

ENDPOINT = "/api/v1/auth/sso/companion/complete"


class CompanionLoginRequest(BaseModel):
    # No NFC normalization: these exact octets are part of the signed digest.
    model_config = ConfigDict(extra="forbid", strict=True)
    provider_alias: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    nonce: str = Field(min_length=20, max_length=512, pattern=r"^[A-Za-z0-9_-]+$")


def request_hash(request: CompanionLoginRequest, access_token: str, id_token: str) -> str:
    wire = json.dumps(
        [request.provider_alias, request.nonce, access_token, id_token],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(wire).digest()).rstrip(b"=").decode("ascii")


def verify_assertion(assertion: str, digest: str, provider_alias: str) -> tuple[str, str, int]:
    """Only pinned deployment keys; token headers never nominate key sources."""
    if (settings.require_auth_mode() != "sso" or not settings.keycloak_enabled
            or not settings.keycloak_companion_login_clients):
        raise AuthenticationError("Companion login is unavailable")
    if not assertion or len(assertion) > 16_384:
        raise AuthenticationError()
    try:
        header = jwt.get_unverified_header(assertion)
        if set(header) != {"alg", "typ", "kid"} or header.get("alg") != "RS256" or header.get("typ") != "JWT":
            raise AuthenticationError()
        unverified = jwt.decode(assertion, options={"verify_signature": False})
        client_id = unverified.get("iss")
        if not isinstance(client_id, str) or not isinstance(header.get("kid"), str):
            raise AuthenticationError()
        client = settings.keycloak_companion_login_clients.get(client_id)
        if client is None or provider_alias not in client.provider_aliases:
            raise AuthenticationError()
        key = client.public_keys.get(header["kid"])
        if not key:
            raise AuthenticationError()
        claims = jwt.decode(
            assertion, key, algorithms=["RS256"], issuer=client_id,
            audience=settings.public_base_url.rstrip("/") + ENDPOINT,
            options={"require": ["iss", "sub", "aud", "iat", "exp", "jti", "request_hash"], "strict_aud": True},
        )
        now = int(time.time())
        if (claims["sub"] != client_id or type(claims["iat"]) is not int or type(claims["exp"]) is not int
                or not now - 60 <= claims["iat"] <= now or not claims["iat"] < claims["exp"] <= claims["iat"] + 60):
            raise AuthenticationError()
        jti = claims["jti"]
        if not isinstance(jti, str) or str(uuid.UUID(jti)) != jti:
            raise AuthenticationError()
        if not isinstance(claims["request_hash"], str) or not secrets.compare_digest(claims["request_hash"], digest):
            raise AuthenticationError()
        return client_id, jti, claims["exp"]
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, UnicodeError):
        raise AuthenticationError() from None


async def _consume_assertion(client_id: str, jti: str, expires_at: int) -> None:
    # Commit independently so even refused/failed account completions consume
    # their assertion. A retry must use a freshly signed jti.
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM companion_login_assertions WHERE expires_at <= NOW()")
            inserted = await conn.fetchval(
                """INSERT INTO companion_login_assertions(client_id,jti,expires_at)
                   SELECT $1,$2,$3::timestamptz + INTERVAL '60 seconds'
                   WHERE $3::timestamptz > clock_timestamp()
                   ON CONFLICT DO NOTHING RETURNING jti""",
                client_id, jti, datetime.fromtimestamp(expires_at, timezone.utc),
            )
            if inserted is None:
                raise AuthenticationError("Companion login assertion was already used")


async def complete_companion_login(
    request: CompanionLoginRequest, access_token: str, id_token: str, assertion: str,
) -> dict:
    if any(not value or len(value) > 16_384 or not value.isascii() for value in (access_token, id_token)):
        raise AuthenticationError()
    digest = request_hash(request, access_token, id_token)
    client_id, jti, expiry = verify_assertion(assertion, digest, request.provider_alias)
    oidc = get_keycloak_oidc()
    principal = await oidc.verify_access_token(access_token, settings.api_oauth_audience_effective, route_profile="api")
    if principal is None or principal.claims.get("azp") != client_id:
        raise AuthenticationError()
    provider = principal.claims.get("identity_provider")
    if local_realm.is_local_alias(request.provider_alias):
        if provider is not None:
            raise AuthenticationError()
    elif provider != request.provider_alias:
        raise AuthenticationError()
    id_claims = await oidc.verify_browser_id_token(
        id_token, expected_nonce=request.nonce, access_token=access_token,
        expected_provider_alias=request.provider_alias, client_id=client_id,
    )
    if (id_claims.get("aud") not in (client_id, [client_id])
            or id_claims.get("sub") != principal.subject or id_claims.get("iss") != principal.issuer
            or id_claims.get("sid") != principal.claims.get("sid")):
        raise AuthenticationError()
    await _consume_assertion(client_id, jti, expiry)
    # Use the same public account projection boundary as AKB's own callback:
    # It owns account transactions, pending admissions, role sync and denials.
    outcome = await project_verified_principal_with_reason(principal, provider_alias=request.provider_alias)
    user = outcome.user
    if user is None:
        denials = {
            "membership_required": MembershipRequiredError,
            "account_suspended": AccountSuspendedError,
            "identity_conflict": ExternalIdentityConflictError,
        }
        denial = denials.get(outcome.refusal_code or "")
        if denial is not None:
            raise denial()
        raise AuthenticationError("Companion login could not be completed")
    return {"user": {
        "id": user.user_id, "username": user.username,
        "email": user.email, "display_name": user.display_name,
        "is_admin": user.is_admin,
    }}
