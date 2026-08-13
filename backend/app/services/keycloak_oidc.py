"""Pinned Keycloak verification plus separated browser OIDC primitives.

Phase 1 request authorization uses :meth:`verify_access_token` only. The
route-selected ``keycloak-access-v1`` profile pins RS256, issuer, JWKS, route
audience, token kind, and required claims before exact ``(issuer, subject)``
projection. It never treats an ID token as an API access token.

The dedicated Phase 2 product-admin client uses its own authorization-code,
PKCE, nonce, ID-token, and logout profile. Ordinary-user browser helpers remain
dormant candidates for Phase 4. This module has no AKB human-session issuance
or account-adoption entry point. See
``docs/designs/keycloak-oidc/00-overview.md``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass
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

# OIDC browser scopes. The admin client uses them now; ordinary-user browser
# routes remain staged for Phase 4.
_SCOPE = "openid profile email"
_TOKEN_SUPPLIED_KEY_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})
_SERVICE_ACCOUNT_CLAIMS = frozenset({"client_id", "clientId", "clientHost", "clientAddress"})
_JWKS_REFRESH_COOLDOWN_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AdminAuthorizationRequest:
    location: str
    browser_binding: str


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
            key,
            kind,
            json.dumps(payload),
            expires_at,
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
            key,
            kind,
        )
    if row is None:
        return None
    payload = row["payload"]
    # asyncpg returns JSONB as str unless a codec is registered.
    return json.loads(payload) if isinstance(payload, str) else payload


async def _store_consume_admin_bound(
    key: str,
    browser_binding_hash: str,
) -> dict | None:
    """Atomically consume admin state only when its browser binding matches."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM oidc_transients
             WHERE key = $1
               AND kind = 'admin-state-v1'
               AND expires_at > NOW()
               AND payload->>'browser_binding_hash' = $2
            RETURNING payload
            """,
            key,
            browser_binding_hash,
        )
    if row is None:
        return None
    payload = row["payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


class KeycloakOIDC:
    """Pinned verifier with isolated admin and ordinary browser profiles."""

    def __init__(self) -> None:
        # JWKS is cached in-process. Unknown kids may request one bounded,
        # single-flight refresh per cooldown window so attacker-controlled
        # headers cannot amplify unauthenticated traffic to Keycloak.
        self._jwks: dict[str, Any] | None = None
        self._jwks_refresh_lock = asyncio.Lock()
        self._jwks_refresh_attempt_at: float | None = None
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
        return client_id.strip() if client_id and client_id.strip() else settings.keycloak_client_id

    async def begin_login(self, redirect_path: str = "/", *, client_id: str | None = None) -> str:
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
            state,
            "state",
            payload,
            ttl_secs=600,  # 10 min to complete the Keycloak login screen
        )
        return f"{settings.keycloak_authorization_endpoint}?{urllib.parse.urlencode(params)}"

    async def consume_state(self, state: str) -> dict | None:
        """Verify+consume the CSRF state. Returns {redirect_path, code_verifier?}."""
        return await _store_consume(state, "state")

    async def begin_admin_login(self) -> AdminAuthorizationRequest:
        """Build the dedicated product-admin authorization request.

        The admin client always uses PKCE in addition to its confidential
        client authentication. Its state/nonce namespace cannot be consumed by
        the ordinary browser-flow helpers.
        """
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = self._make_code_verifier()
        browser_binding = secrets.token_urlsafe(32)
        await _store_issue(
            state,
            "admin-state-v1",
            {
                "code_verifier": verifier,
                "nonce": nonce,
                "browser_binding_hash": hashlib.sha256(
                    browser_binding.encode("ascii")
                ).hexdigest(),
            },
            ttl_secs=600,
        )
        params = {
            "client_id": settings.keycloak_admin_client_id,
            "redirect_uri": settings.keycloak_admin_redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": self._make_code_challenge(verifier),
            "code_challenge_method": "S256",
            # Product administration is a recovery surface.  Never satisfy it
            # from a pre-existing realm/broker SSO cookie; force a fresh native
            # credential ceremony whose exact AMR is verified below.
            "prompt": "login",
            "max_age": "0",
        }
        return AdminAuthorizationRequest(
            location=(
                f"{settings.keycloak_authorization_endpoint}?"
                f"{urllib.parse.urlencode(params)}"
            ),
            browser_binding=browser_binding,
        )

    async def consume_admin_state(
        self,
        state: str,
        browser_binding: str,
    ) -> dict | None:
        """Consume state only in the browser that initiated the login."""
        if not 20 <= len(browser_binding) <= 512:
            return None
        actual_hash = hashlib.sha256(browser_binding.encode("utf-8")).hexdigest()
        payload = await _store_consume_admin_bound(state, actual_hash)
        if not isinstance(payload, dict):
            return None
        expected_hash = payload.pop("browser_binding_hash", None)
        if not isinstance(expected_hash, str) or not secrets.compare_digest(
            expected_hash,
            actual_hash,
        ):
            return None
        return payload

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
            resp = await self._client().post(settings.keycloak_token_endpoint, data=data)
        except httpx.HTTPError as e:
            logger.error("Keycloak token exchange network error: %s", e)
            raise AKBError("Keycloak unreachable during token exchange", status_code=502) from e

        if resp.status_code != 200:
            logger.warning(
                "Keycloak token exchange failed: %s %s",
                resp.status_code,
                resp.text[:500],
            )
            # A bad/expired/replayed code is a client-side auth failure.
            raise AuthenticationError("Authorization code exchange failed")
        return resp.json()

    async def exchange_admin_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange one admin-client code without logging token material."""
        if not code or not code_verifier:
            raise AuthenticationError("Authorization code exchange failed")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.keycloak_admin_redirect_uri,
            "client_id": settings.keycloak_admin_client_id,
            "client_secret": settings.keycloak_admin_client_secret,
            "code_verifier": code_verifier,
        }
        try:
            response = await self._client().post(
                settings.keycloak_token_endpoint,
                data=data,
            )
        except httpx.HTTPError as exc:
            logger.error("Keycloak admin token exchange network error")
            raise AKBError(
                "Keycloak unreachable during token exchange",
                status_code=502,
            ) from exc
        if response.status_code != 200:
            logger.warning(
                "Keycloak admin token exchange failed with status %s",
                response.status_code,
            )
            raise AuthenticationError("Authorization code exchange failed")
        try:
            payload = response.json()
        except TypeError, ValueError:
            raise AuthenticationError("Authorization code exchange failed") from None
        if not isinstance(payload, dict):
            raise AuthenticationError("Authorization code exchange failed")
        return payload

    # ── ID-token verification (admin active; ordinary Phase 4 candidate) ──
    async def _fetch_jwks(self, *, force: bool = False) -> dict[str, Any]:
        if self._jwks is not None and not force:
            return self._jwks
        async with self._jwks_refresh_lock:
            if self._jwks is not None and not force:
                return self._jwks

            now = time.monotonic()
            if (
                self._jwks_refresh_attempt_at is not None
                and now - self._jwks_refresh_attempt_at < _JWKS_REFRESH_COOLDOWN_SECONDS
            ):
                if self._jwks is not None:
                    return self._jwks
                raise AKBError(
                    "Keycloak public keys are temporarily unavailable",
                    status_code=502,
                )

            # Record attempts, not just successes. A Keycloak outage must not
            # turn malformed bearer traffic into an upstream request storm.
            self._jwks_refresh_attempt_at = now
            try:
                resp = await self._client().get(settings.keycloak_jwks_uri)
            except httpx.HTTPError as e:
                logger.error("Keycloak JWKS fetch network error: %s", e)
                raise AKBError("Keycloak unreachable fetching JWKS", status_code=502) from e
            if resp.status_code != 200:
                logger.error("Keycloak JWKS fetch failed: %s", resp.status_code)
                raise AKBError("Failed to fetch Keycloak public keys", status_code=502)
            try:
                candidate = resp.json()
            except (TypeError, ValueError) as e:
                raise AKBError("Keycloak returned invalid public keys", status_code=502) from e
            if not isinstance(candidate, dict) or not isinstance(candidate.get("keys"), list):
                raise AKBError("Keycloak returned invalid public keys", status_code=502)
            self._jwks = candidate
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

    async def verify_id_token(self, id_token: str, *, client_id: str | None = None) -> dict[str, Any]:
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
        if key.get("kty") != "RSA" or key.get("use") != "sig" or key.get("alg") != "RS256":
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
        except (jwt.PyJWTError, TypeError, ValueError) as e:
            logger.warning("Keycloak ID token verification failed: %s", e)
            raise AuthenticationError(f"Invalid ID token: {e}") from e

        return claims

    async def verify_admin_id_token(
        self,
        id_token: str,
        *,
        expected_nonce: str,
    ) -> dict[str, Any]:
        """Verify the exact Keycloak ID-token profile for `/admin`."""
        claims = await self.verify_id_token(
            id_token,
            client_id=settings.keycloak_admin_client_id,
        )
        required_string_limits = {
            "iss": 2048,
            "sub": 1024,
            "azp": 255,
            "sid": 255,
            "nonce": 512,
        }
        if any(
            not isinstance(claims.get(name), str)
            or not claims[name].strip()
            or len(claims[name]) > limit
            for name, limit in required_string_limits.items()
        ):
            raise AuthenticationError("Invalid admin identity token")
        if (
            not isinstance(expected_nonce, str)
            or not 20 <= len(expected_nonce) <= 512
            or not expected_nonce.isascii()
            or not claims["nonce"].isascii()
            or not secrets.compare_digest(claims["nonce"], expected_nonce)
        ):
            raise AuthenticationError("Invalid admin identity token")
        if claims["azp"] != settings.keycloak_admin_client_id:
            raise AuthenticationError("Invalid admin identity token")
        if (
            type(claims.get("iat")) is not int
            or type(claims.get("exp")) is not int
            or claims["exp"] <= claims["iat"]
        ):
            raise AuthenticationError("Invalid admin identity token")
        if _SERVICE_ACCOUNT_CLAIMS.intersection(claims):
            raise AuthenticationError("Invalid admin identity token")
        if claims.get("amr") != ["pwd"]:
            raise AuthenticationError("Invalid admin identity token")
        preferred_username = claims.get("preferred_username")
        if preferred_username is not None and (
            not isinstance(preferred_username, str) or preferred_username.startswith("service-account-")
        ):
            raise AuthenticationError("Invalid admin identity token")
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
        if key.get("kty") != "RSA" or key.get("use") != "sig" or key.get("alg") != "RS256":
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
        except (jwt.PyJWTError, TypeError, ValueError) as e:
            logger.debug("Keycloak access token verification failed: %s", e)
            return None

        required_strings = ("iss", "sub", "jti", "typ", "azp", "sid", "scope")
        if any(not isinstance(claims.get(name), str) or not claims[name].strip() for name in required_strings):
            return None
        if claims["typ"] != "Bearer":
            return None
        for optional_name in ("email", "name", "preferred_username"):
            optional_value = claims.get(optional_name)
            if optional_value is not None and not isinstance(optional_value, str):
                return None
        if "email_verified" in claims and type(claims["email_verified"]) is not bool:
            return None
        # The dedicated browser-admin client is an authentication proof only.
        # Its access token must never become an API or MCP resource credential,
        # even on the MCP profile where dynamic client IDs are otherwise valid.
        if claims["azp"] == settings.keycloak_admin_client_id:
            return None
        if route_profile == "api" and claims["azp"] not in settings.keycloak_human_client_ids:
            return None
        if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int:
            return None
        if claims["exp"] <= claims["iat"]:
            return None
        raw_audience = claims.get("aud")
        if isinstance(raw_audience, str):
            audiences = {raw_audience}
        elif isinstance(raw_audience, list) and all(isinstance(item, str) and item for item in raw_audience):
            audiences = set(raw_audience)
        else:
            return None
        other_route_audience = (
            settings.mcp_oauth_audience_effective if route_profile == "api" else settings.api_oauth_audience_effective
        )
        if other_route_audience and other_route_audience != audience and other_route_audience in audiences:
            return None
        if _SERVICE_ACCOUNT_CLAIMS.intersection(claims):
            return None
        preferred_username = claims.get("preferred_username")
        if isinstance(preferred_username, str) and preferred_username.startswith("service-account-"):
            return None

        return VerifiedPrincipal(
            profile_id=KEYCLOAK_ACCESS_V1,
            issuer=claims["iss"],
            subject=claims["sub"],
            credential_type="access_token",
            claims=claims,
            audience=audience,
        )

    # ── Logout URL construction ──────────────────────────────────────
    def logout_url(self, id_token_hint: str | None, post_logout_redirect: str | None) -> str:
        params: dict[str, str] = {}
        if post_logout_redirect:
            params["post_logout_redirect_uri"] = post_logout_redirect
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        else:
            params["client_id"] = settings.keycloak_client_id
        return f"{settings.keycloak_end_session_endpoint}?{urllib.parse.urlencode(params)}"

    def admin_logout_url(self) -> str:
        params = {
            "client_id": settings.keycloak_admin_client_id,
            "post_logout_redirect_uri": settings.keycloak_admin_post_logout_redirect_uri,
        }
        return f"{settings.keycloak_end_session_endpoint}?{urllib.parse.urlencode(params)}"


# Lazy module-level singleton.
_service: KeycloakOIDC | None = None


def get_keycloak_oidc() -> KeycloakOIDC:
    global _service
    if _service is None:
        _service = KeycloakOIDC()
    return _service
