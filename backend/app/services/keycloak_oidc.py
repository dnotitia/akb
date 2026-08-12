"""Pinned Keycloak verification plus dormant browser OIDC primitives.

Phase 1 request authorization uses :meth:`verify_access_token` only. The
route-selected ``keycloak-access-v1`` profile pins RS256, issuer, JWKS, route
audience, token kind, and required claims before exact ``(issuer, subject)``
projection. It never treats an ID token as an API access token.

Authorization-code, ID-token, and logout helpers remain internal candidates
for the Phase 4 server-side browser-session design. No Phase 1 production
route calls them, and this module has no AKB human-session issuance or account
adoption entry point. See ``docs/designs/keycloak-oidc/00-overview.md``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, AuthenticationError
from app.services.auth_verifier_profiles import (
    KEYCLOAK_ACCESS_V1,
    KeycloakRouteProfile,
    VerifiedPrincipal,
)

logger = logging.getLogger("akb.keycloak")

# Reserved Phase 4 authorization-code scopes. The staged Phase 1 browser
# routes never issue an authorization request.
_SCOPE = "openid profile email"
_TOKEN_SUPPLIED_KEY_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})
_SERVICE_ACCOUNT_CLAIMS = frozenset(
    {"client_id", "clientId", "clientHost", "clientAddress"}
)


# ── Transient store (oidc_transients table) ──────────────────────────
#
# Legacy single-use, TTL-bounded transient storage retained for schema
# compatibility and possible Phase 4 reuse. Staged Phase 1 browser routes do
# not issue or consume these records. Consume remains atomic DELETE … RETURNING.


async def _store_issue(key: str, kind: str, payload: dict, ttl_secs: int) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_secs)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Opportunistic GC of expired rows — cheap with the expiry index,
        # keeps the table from accumulating abandoned login attempts.
        await conn.execute("DELETE FROM oidc_transients WHERE expires_at <= NOW()")
        await conn.execute(
            """
            INSERT INTO oidc_transients (key, kind, payload, expires_at)
            VALUES ($1, $2, $3::jsonb, $4)
            """,
            key, kind, json.dumps(payload), expires_at,
        )


async def _store_consume(key: str, kind: str) -> dict | None:
    """Atomically fetch+delete a non-expired transient. None if absent/expired."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM oidc_transients
             WHERE key = $1 AND kind = $2 AND expires_at > NOW()
            RETURNING payload
            """,
            key, kind,
        )
    if row is None:
        return None
    payload = row["payload"]
    # asyncpg returns JSONB as str unless a codec is registered.
    return json.loads(payload) if isinstance(payload, str) else payload


class KeycloakOIDC:
    """Pinned verifier with dormant, PostgreSQL-backed browser helpers."""

    def __init__(self) -> None:
        # JWKS cached in-process; refetched on key rotation (kid miss).
        self._jwks: dict[str, Any] | None = None
        self._http: httpx.AsyncClient | None = None

    # ── HTTP client (honors verify_ssl) ──────────────────────────────
    def _client(self) -> httpx.AsyncClient:
        # A dedicated client (not the shared embedding/LLM pool) because
        # verify must follow keycloak_verify_ssl, which is a per-client
        # setting in httpx. Reused across calls for connection keep-alive.
        if self._http is None:
            self._http = httpx.AsyncClient(
                verify=settings.keycloak_verify_ssl,
                timeout=httpx.Timeout(15.0, connect=10.0),
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── PKCE helpers (RFC 7636) ──────────────────────────────────────
    @staticmethod
    def _make_code_verifier() -> str:
        return secrets.token_urlsafe(64)[:128]

    @staticmethod
    def _make_code_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    # ── Authorization request ────────────────────────────────────────
    @staticmethod
    def _effective_client_id(client_id: str | None) -> str:
        """Resolve an optional server-selected client to the legacy default."""
        return (
            client_id.strip()
            if client_id and client_id.strip()
            else settings.keycloak_client_id
        )

    async def begin_login(
        self, redirect_path: str = "/", *, client_id: str | None = None
    ) -> str:
        """Build dormant Phase 4 authorization state and redirect URL.

        No Phase 1 production route calls this helper. Any future caller must
        validate ``redirect_path`` against the server-side browser-session
        design before enabling it.
        """
        selected_client_id = self._effective_client_id(client_id)
        state = secrets.token_urlsafe(32)
        payload: dict[str, str] = {
            "redirect_path": redirect_path,
            "client_id": selected_client_id,
        }
        params: dict[str, str] = {
            "client_id": selected_client_id,
            "redirect_uri": settings.keycloak_redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "state": state,
        }
        if settings.keycloak_public_client:
            verifier = self._make_code_verifier()
            payload["code_verifier"] = verifier
            params["code_challenge"] = self._make_code_challenge(verifier)
            params["code_challenge_method"] = "S256"

        await _store_issue(
            state, "state", payload,
            ttl_secs=600,  # 10 min to complete the Keycloak login screen
        )
        return f"{settings.keycloak_authorization_endpoint}?{urllib.parse.urlencode(params)}"

    async def consume_state(self, state: str) -> dict | None:
        """Verify+consume the CSRF state. Returns {redirect_path, code_verifier?}."""
        return await _store_consume(state, "state")

    # ── Dormant authorization-code exchange (Phase 4 candidate) ─────
    async def exchange_code_for_tokens(
        self,
        code: str,
        code_verifier: str | None,
        *,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        selected_client_id = self._effective_client_id(client_id)
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.keycloak_redirect_uri,
            "client_id": selected_client_id,
        }
        if settings.keycloak_public_client:
            if code_verifier:
                data["code_verifier"] = code_verifier
        else:
            data["client_secret"] = settings.keycloak_client_secret

        try:
            resp = await self._client().post(
                settings.keycloak_token_endpoint, data=data
            )
        except httpx.HTTPError as e:
            logger.error("Keycloak token exchange network error: %s", e)
            raise AKBError("Keycloak unreachable during token exchange", status_code=502) from e

        if resp.status_code != 200:
            logger.warning(
                "Keycloak token exchange failed: %s %s",
                resp.status_code, resp.text[:500],
            )
            # A bad/expired/replayed code is a client-side auth failure.
            raise AuthenticationError("Authorization code exchange failed")
        return resp.json()

    # ── Dormant ID-token verification (Phase 4 candidate) ────────────
    async def _fetch_jwks(self, *, force: bool = False) -> dict[str, Any]:
        if self._jwks is not None and not force:
            return self._jwks
        try:
            resp = await self._client().get(settings.keycloak_jwks_uri)
        except httpx.HTTPError as e:
            logger.error("Keycloak JWKS fetch network error: %s", e)
            raise AKBError("Keycloak unreachable fetching JWKS", status_code=502) from e
        if resp.status_code != 200:
            logger.error("Keycloak JWKS fetch failed: %s", resp.status_code)
            raise AKBError("Failed to fetch Keycloak public keys", status_code=502)
        self._jwks = resp.json()
        return self._jwks

    @staticmethod
    def _find_key(jwks: dict[str, Any], kid: str) -> dict | None:
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            return None
        return next(
            (key for key in keys if isinstance(key, dict) and key.get("kid") == kid),
            None,
        )

    async def verify_id_token(
        self, id_token: str, *, client_id: str | None = None
    ) -> dict[str, Any]:
        """Verify a Keycloak ID token locally and return its claims.

        Validates signature (RS256), audience (client_id), issuer (realm),
        and expiry. Refetches JWKS once if the token's ``kid`` is unknown
        (key rotation) before giving up.
        """
        selected_client_id = self._effective_client_id(client_id)
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as e:
            raise AuthenticationError(f"Malformed ID token: {e}") from e

        if header.get("alg") != "RS256" or header.get("typ") != "JWT":
            raise AuthenticationError("ID token does not match the RS256 JWT profile")
        if _TOKEN_SUPPLIED_KEY_HEADERS.intersection(header):
            raise AuthenticationError("ID token contains an untrusted key-source header")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationError("ID token missing kid header")

        jwks = await self._fetch_jwks()
        key = self._find_key(jwks, kid)
        if key is None:
            # Unknown kid — Keycloak likely rotated keys. Refetch once.
            jwks = await self._fetch_jwks(force=True)
            key = self._find_key(jwks, kid)
        if key is None:
            raise AuthenticationError("No matching Keycloak public key for token")
        if (
            key.get("kty") != "RSA"
            or key.get("use") != "sig"
            or key.get("alg") != "RS256"
        ):
            raise AuthenticationError("Keycloak ID-token signing key is not allowed")

        try:
            public_key = RSAAlgorithm.from_jwk(json.dumps(key))
            claims = jwt.decode(
                id_token,
                cast(Any, public_key),
                algorithms=["RS256"],
                audience=selected_client_id,
                issuer=settings.keycloak_issuer,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.InvalidTokenError as e:
            logger.warning("Keycloak ID token verification failed: %s", e)
            raise AuthenticationError(f"Invalid ID token: {e}") from e

        return claims

    # ── Route-selected access-token verification ────────────────────
    async def verify_access_token(
        self,
        token: str,
        audience: str,
        *,
        route_profile: KeycloakRouteProfile,
    ) -> VerifiedPrincipal | None:
        """Verify one route-selected keycloak-access-v1 credential.

        The caller supplies the already-selected API or MCP audience. Token
        headers may only confirm this fixed RS256/JWKS profile; they never
        select an algorithm, issuer, key source, or fallback verifier.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as e:
            logger.debug("Keycloak access token: malformed header (%s)", e)
            return None

        if route_profile not in ("api", "mcp"):
            return None
        if header.get("alg") != "RS256" or header.get("typ") != "JWT":
            return None
        if _TOKEN_SUPPLIED_KEY_HEADERS.intersection(header):
            return None
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return None

        try:
            jwks = await self._fetch_jwks()
            key = self._find_key(jwks, kid)
            if key is None:
                jwks = await self._fetch_jwks(force=True)
                key = self._find_key(jwks, kid)
        except AKBError as e:
            logger.warning("Keycloak access token: JWKS unavailable (%s)", e)
            return None
        if key is None:
            return None
        if (
            key.get("kty") != "RSA"
            or key.get("use") != "sig"
            or key.get("alg") != "RS256"
        ):
            return None

        try:
            public_key = RSAAlgorithm.from_jwk(json.dumps(key))
            claims = jwt.decode(
                token,
                cast(Any, public_key),
                algorithms=["RS256"],
                audience=audience,
                issuer=settings.keycloak_issuer,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "jti",
                        "aud",
                        "iss",
                        "sub",
                        "typ",
                        "azp",
                        "sid",
                        "scope",
                    ]
                },
            )
        except (jwt.InvalidTokenError, TypeError, ValueError) as e:
            logger.debug("Keycloak access token verification failed: %s", e)
            return None

        required_strings = ("iss", "sub", "jti", "typ", "azp", "sid", "scope")
        if any(
            not isinstance(claims.get(name), str) or not claims[name].strip()
            for name in required_strings
        ):
            return None
        if claims["typ"] != "Bearer":
            return None
        if (
            route_profile == "api"
            and claims["azp"] not in settings.keycloak_human_client_ids
        ):
            return None
        if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int:
            return None
        if claims["exp"] <= claims["iat"]:
            return None
        raw_audience = claims.get("aud")
        if isinstance(raw_audience, str):
            audiences = {raw_audience}
        elif isinstance(raw_audience, list) and all(
            isinstance(item, str) and item for item in raw_audience
        ):
            audiences = set(raw_audience)
        else:
            return None
        other_route_audience = (
            settings.mcp_oauth_audience_effective
            if route_profile == "api"
            else settings.api_oauth_audience_effective
        )
        if (
            other_route_audience
            and other_route_audience != audience
            and other_route_audience in audiences
        ):
            return None
        if _SERVICE_ACCOUNT_CLAIMS.intersection(claims):
            return None
        preferred_username = claims.get("preferred_username")
        if isinstance(preferred_username, str) and preferred_username.startswith(
            "service-account-"
        ):
            return None

        return VerifiedPrincipal(
            profile_id=KEYCLOAK_ACCESS_V1,
            issuer=claims["iss"],
            subject=claims["sub"],
            credential_type="access_token",
            claims=claims,
            audience=audience,
        )

    # ── Dormant logout URL construction (Phase 4 candidate) ─────────
    def logout_url(self, id_token_hint: str | None, post_logout_redirect: str | None) -> str:
        params: dict[str, str] = {}
        if post_logout_redirect:
            params["post_logout_redirect_uri"] = post_logout_redirect
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        else:
            params["client_id"] = settings.keycloak_client_id
        return f"{settings.keycloak_end_session_endpoint}?{urllib.parse.urlencode(params)}"


# Lazy module-level singleton.
_service: KeycloakOIDC | None = None


def get_keycloak_oidc() -> KeycloakOIDC:
    global _service
    if _service is None:
        _service = KeycloakOIDC()
    return _service
