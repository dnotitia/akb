"""Pinned Keycloak verification plus separated browser OIDC primitives.

Request authorization uses :meth:`verify_access_token` only. The
route-selected ``keycloak-access-v1`` profile pins RS256, issuer, JWKS, route
audience, token kind, and required claims before exact ``(issuer, subject)``
projection. It never treats an ID token as an API access token.

The dedicated product-admin client uses its own authorization-code, PKCE,
nonce, ID-token, and logout profile. Ordinary-user browser helpers support the
separate encrypted AKB browser-session service; this module itself has no AKB
human-session issuance or account-adoption entry point. See
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
    KEYCLOAK_SERVICE_AUTHORITY_V1,
    KeycloakRouteProfile,
    VerifiedPrincipal,
)
from app.sso.providers.keycloak_oidc import ProviderDefinitionError, validate_alias

logger = logging.getLogger("akb.keycloak")

# Fixed OIDC browser scopes shared by the separated admin and ordinary clients.
_SCOPE = "openid profile email"
_TOKEN_SUPPLIED_KEY_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})
_SERVICE_ACCOUNT_CLAIMS = frozenset({"client_id", "clientId", "clientHost", "clientAddress"})
# Claims that describe a person. A client-credentials token has none of them,
# so their presence means the bearer came from some other flow on the client.
_HUMAN_PROFILE_CLAIMS = frozenset({"email", "email_verified", "name"})
_JWKS_REFRESH_COOLDOWN_SECONDS = 30.0
_BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


@dataclass(frozen=True, slots=True)
class AdminAuthorizationRequest:
    location: str
    browser_binding: str


@dataclass(frozen=True, slots=True)
class BrowserAuthorizationRequest:
    location: str
    browser_binding: str


# ── Transient store (oidc_transients table) ──────────────────────────
#
# Namespaced single-use, TTL-bounded transient storage for both browser flows.
# Consume remains atomic DELETE … RETURNING.


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


async def _store_consume_browser_bound(
    key: str,
    browser_binding_hash: str,
) -> dict | None:
    """Atomically consume ordinary-user state in its initiating browser."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM oidc_transients
             WHERE key = $1
               AND kind = 'browser-state-v1'
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


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


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

    async def begin_browser_login(
        self,
        redirect_path: str,
        *,
        provider_alias: str,
    ) -> BrowserAuthorizationRequest:
        """Build the ordinary AKB browser's nonce+PKCE authorization request.

        ``redirect_path`` and ``provider_alias`` are validated by the route
        before this method is called. The OIDC client and callback are always
        server-owned; a companion application cannot select either value.
        """
        selected_client_id = settings.keycloak_client_id
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = self._make_code_verifier()
        browser_binding = secrets.token_urlsafe(32)
        payload: dict[str, str] = {
            "redirect_path": redirect_path,
            "provider_alias": provider_alias,
            "client_id": selected_client_id,
            "code_verifier": verifier,
            "nonce": nonce,
            "browser_binding_hash": hashlib.sha256(browser_binding.encode("ascii")).hexdigest(),
        }
        params: dict[str, str] = {
            "client_id": selected_client_id,
            "redirect_uri": settings.keycloak_browser_redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": self._make_code_challenge(verifier),
            "code_challenge_method": "S256",
            "kc_idp_hint": provider_alias,
            # The browser can carry a native Keycloak session from the
            # separate product-admin surface.  Force Keycloak to run the
            # selected broker ceremony again without forwarding prompt=login
            # and unnecessarily defeating the upstream provider's own SSO.
            "max_age": "0",
        }
        await _store_issue(
            state,
            "browser-state-v1",
            payload,
            ttl_secs=600,
        )
        return BrowserAuthorizationRequest(
            location=(f"{settings.keycloak_authorization_endpoint}?{urllib.parse.urlencode(params)}"),
            browser_binding=browser_binding,
        )

    async def consume_browser_state(
        self,
        state: str,
        browser_binding: str,
    ) -> dict | None:
        """Consume user state only from the browser that initiated login."""
        if not 20 <= len(browser_binding) <= 512:
            return None
        actual_hash = hashlib.sha256(browser_binding.encode("utf-8")).hexdigest()
        payload = await _store_consume_browser_bound(state, actual_hash)
        if not isinstance(payload, dict):
            return None
        expected_hash = payload.pop("browser_binding_hash", None)
        if not isinstance(expected_hash, str) or not secrets.compare_digest(
            expected_hash,
            actual_hash,
        ):
            return None
        return payload

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
                "browser_binding_hash": hashlib.sha256(browser_binding.encode("ascii")).hexdigest(),
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
            location=(f"{settings.keycloak_authorization_endpoint}?{urllib.parse.urlencode(params)}"),
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

    # ── Ordinary browser token lifecycle ────────────────────────────
    async def exchange_browser_code(
        self,
        code: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        if not code or not code_verifier:
            raise AuthenticationError("Authorization code exchange failed")
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.keycloak_browser_redirect_uri,
            "client_id": settings.keycloak_client_id,
            "code_verifier": code_verifier,
        }
        if not settings.keycloak_public_client:
            data["client_secret"] = settings.keycloak_client_secret

        try:
            resp = await self._client().post(settings.keycloak_token_endpoint, data=data)
        except httpx.HTTPError as e:
            logger.error("Keycloak token exchange network error: %s", e)
            raise AKBError("Keycloak unreachable during token exchange", status_code=502) from e

        if resp.status_code != 200:
            logger.warning(
                "Keycloak browser token exchange failed with status %s",
                resp.status_code,
            )
            raise AuthenticationError("Authorization code exchange failed")
        try:
            payload = resp.json()
        except TypeError, ValueError:
            raise AuthenticationError("Authorization code exchange failed") from None
        if not isinstance(payload, dict):
            raise AuthenticationError("Authorization code exchange failed")
        return payload

    async def refresh_browser_tokens(self, refresh_token: str) -> dict[str, Any]:
        """Rotate one server-custodied refresh token through Keycloak."""
        if not refresh_token or len(refresh_token) > 16_384:
            raise AuthenticationError("SSO browser session refresh failed")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.keycloak_client_id,
        }
        if not settings.keycloak_public_client:
            data["client_secret"] = settings.keycloak_client_secret
        try:
            response = await self._client().post(
                settings.keycloak_token_endpoint,
                data=data,
            )
        except httpx.HTTPError as exc:
            logger.error("Keycloak browser token refresh network error")
            raise AKBError(
                "Keycloak unreachable during session refresh",
                status_code=502,
            ) from exc
        if response.status_code != 200:
            logger.info(
                "Keycloak browser token refresh rejected with status %s",
                response.status_code,
            )
            raise AuthenticationError("SSO browser session refresh failed")
        try:
            payload = response.json()
        except TypeError, ValueError:
            raise AuthenticationError("SSO browser session refresh failed") from None
        if not isinstance(payload, dict):
            raise AuthenticationError("SSO browser session refresh failed")
        return payload

    async def revoke_browser_refresh_token(self, refresh_token: str) -> bool:
        """Best-effort Keycloak session revocation without exposing material."""
        if not refresh_token or len(refresh_token) > 16_384:
            return False
        data = {
            "refresh_token": refresh_token,
            "client_id": settings.keycloak_client_id,
        }
        if not settings.keycloak_public_client:
            data["client_secret"] = settings.keycloak_client_secret
        try:
            response = await self._client().post(
                settings.keycloak_backchannel_logout_endpoint,
                data=data,
            )
        except httpx.HTTPError:
            logger.warning("Keycloak browser refresh-token revocation was unreachable")
            return False
        if response.status_code not in {200, 204}:
            logger.warning(
                "Keycloak browser refresh-token revocation failed with status %s",
                response.status_code,
            )
            return False
        return True

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

    # ── ID-token verification for separated admin and ordinary clients ──
    async def _fetch_jwks(
        self,
        *,
        force: bool = False,
        observed_jwks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._jwks is not None and not force:
            return self._jwks
        async with self._jwks_refresh_lock:
            # Another request refreshed after this caller missed against its
            # observed cache. Reuse that one result rather than issuing a
            # duplicate request or misclassifying the cooldown as an outage.
            if force and observed_jwks is not None and self._jwks is not observed_jwks:
                if self._jwks is None:
                    raise AKBError(
                        "Keycloak public keys are temporarily unavailable",
                        status_code=502,
                    )
                return self._jwks
            if self._jwks is not None and not force:
                return self._jwks

            now = time.monotonic()
            if (
                self._jwks_refresh_attempt_at is not None
                and now - self._jwks_refresh_attempt_at < _JWKS_REFRESH_COOLDOWN_SECONDS
            ):
                if self._jwks is not None and not force:
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

    @staticmethod
    def _has_pinned_unverified_issuer(token: str) -> bool:
        """Reject an obvious issuer mismatch before touching pinned JWKS.

        This untrusted claim can only reject. It never selects an issuer,
        algorithm, key source, or acceptance path; a matching value still
        requires the complete pinned signature/profile verification below.
        """
        try:
            claims = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_aud": False,
                },
            )
        except (jwt.PyJWTError, TypeError, ValueError):
            return False
        return isinstance(claims, dict) and claims.get("iss") == settings.keycloak_issuer

    async def _resolve_signing_key(self, kid: str) -> dict | None:
        """Resolve one kid with one bounded rotation refresh.

        A miss against a cache whose refresh is currently cooldown-blocked is
        an availability condition, not proof of an invalid signature. A
        refresh completed after this request observed the old cache may be
        reused by every waiter.
        """
        observed_before_fetch = self._jwks
        jwks = await self._fetch_jwks()
        key = self._find_key(jwks, kid)
        if key is not None or observed_before_fetch is None:
            return key
        if jwks is not observed_before_fetch:
            return None
        refreshed = await self._fetch_jwks(
            force=True,
            observed_jwks=observed_before_fetch,
        )
        return self._find_key(refreshed, kid)

    async def verify_id_token(self, id_token: str, *, client_id: str | None = None) -> dict[str, Any]:
        """Verify a Keycloak ID token locally and return its claims.

        Validates signature (RS256), audience (client_id), issuer (realm),
        and expiry. Refetches JWKS once if the token's ``kid`` is unknown
        (key rotation) before giving up.
        """
        if not isinstance(id_token, str) or not 1 <= len(id_token) <= 16_384:
            raise AuthenticationError("Malformed ID token")
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
        if not self._has_pinned_unverified_issuer(id_token):
            raise AuthenticationError("Invalid ID token issuer")

        key = await self._resolve_signing_key(kid)
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
            not isinstance(claims.get(name), str) or not claims[name].strip() or len(claims[name]) > limit
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
        if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int or claims["exp"] <= claims["iat"]:
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

    async def verify_browser_id_token(
        self,
        id_token: str,
        *,
        expected_nonce: str,
        access_token: str,
        expected_provider_alias: str,
    ) -> dict[str, Any]:
        """Verify the ordinary browser ID token and bind it to this exchange."""
        claims = await self.verify_id_token(
            id_token,
            client_id=settings.keycloak_client_id,
        )
        required_string_limits = {
            "iss": 2048,
            "sub": 1024,
            "azp": 255,
            "sid": 255,
            "nonce": 512,
            "at_hash": 512,
            "identity_provider": 63,
        }
        if any(
            not isinstance(claims.get(name), str) or not claims[name].strip() or len(claims[name]) > limit
            for name, limit in required_string_limits.items()
        ):
            raise AuthenticationError("Invalid browser identity token")
        if claims["iss"] != settings.keycloak_issuer:
            raise AuthenticationError("Invalid browser identity token")
        if claims["azp"] != settings.keycloak_client_id:
            raise AuthenticationError("Invalid browser identity token")
        try:
            actual_provider_alias = validate_alias(claims["identity_provider"])
            selected_provider_alias = validate_alias(expected_provider_alias)
        except (ProviderDefinitionError, TypeError):
            raise AuthenticationError("Invalid browser identity token") from None
        if actual_provider_alias != selected_provider_alias:
            raise AuthenticationError("Invalid browser identity token")
        if (
            not isinstance(expected_nonce, str)
            or not 20 <= len(expected_nonce) <= 512
            or not expected_nonce.isascii()
            or not claims["nonce"].isascii()
            or not secrets.compare_digest(claims["nonce"], expected_nonce)
        ):
            raise AuthenticationError("Invalid browser identity token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthenticationError("Invalid browser identity token")
        digest = hashlib.sha256(access_token.encode("ascii")).digest()
        expected_at_hash = _encode_base64url(digest[: len(digest) // 2])
        if not secrets.compare_digest(claims["at_hash"], expected_at_hash):
            raise AuthenticationError("Invalid browser identity token")
        if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int or claims["exp"] <= claims["iat"]:
            raise AuthenticationError("Invalid browser identity token")
        if _SERVICE_ACCOUNT_CLAIMS.intersection(claims):
            raise AuthenticationError("Invalid browser identity token")
        preferred_username = claims.get("preferred_username")
        if preferred_username is not None and (
            not isinstance(preferred_username, str) or preferred_username.startswith("service-account-")
        ):
            raise AuthenticationError("Invalid browser identity token")
        return claims

    async def verify_backchannel_logout_token(
        self,
        logout_token: str,
    ) -> dict[str, Any]:
        """Verify the fixed Keycloak/OIDC Back-Channel Logout profile."""
        if not isinstance(logout_token, str) or not 1 <= len(logout_token) <= 16_384:
            raise AuthenticationError("Invalid back-channel logout token")
        try:
            header = jwt.get_unverified_header(logout_token)
        except jwt.InvalidTokenError:
            raise AuthenticationError("Invalid back-channel logout token") from None
        if header.get("alg") != "RS256" or header.get("typ") not in {
            "JWT",
            "logout+jwt",
        }:
            raise AuthenticationError("Invalid back-channel logout token")
        if _TOKEN_SUPPLIED_KEY_HEADERS.intersection(header):
            raise AuthenticationError("Invalid back-channel logout token")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationError("Invalid back-channel logout token")
        if not self._has_pinned_unverified_issuer(logout_token):
            raise AuthenticationError("Invalid back-channel logout token")

        # Preserve availability as a 5xx so Keycloak can retry delivery. A
        # pinned-JWKS outage is not evidence that the signed logout token is
        # invalid and must not be flattened into the route's bounded 400.
        key = await self._resolve_signing_key(kid)
        if key is None or key.get("kty") != "RSA" or key.get("use") != "sig" or key.get("alg") != "RS256":
            raise AuthenticationError("Invalid back-channel logout token")
        try:
            public_key = RSAAlgorithm.from_jwk(json.dumps(key))
            claims = jwt.decode(
                logout_token,
                cast(Any, public_key),
                algorithms=["RS256"],
                audience=settings.keycloak_client_id,
                issuer=settings.keycloak_issuer,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "jti",
                        "aud",
                        "iss",
                        "sid",
                        "events",
                        "typ",
                    ]
                },
            )
        except jwt.PyJWTError, TypeError, ValueError:
            raise AuthenticationError("Invalid back-channel logout token") from None

        if claims.get("typ") != "Logout" or "nonce" in claims:
            raise AuthenticationError("Invalid back-channel logout token")
        events = claims.get("events")
        if (
            not isinstance(events, dict)
            or _BACKCHANNEL_LOGOUT_EVENT not in events
            or not isinstance(events[_BACKCHANNEL_LOGOUT_EVENT], dict)
        ):
            raise AuthenticationError("Invalid back-channel logout token")
        required_limits = {"iss": 2048, "sid": 255, "jti": 512}
        if any(
            not isinstance(claims.get(name), str) or not claims[name] or len(claims[name]) > maximum
            for name, maximum in required_limits.items()
        ):
            raise AuthenticationError("Invalid back-channel logout token")
        subject = claims.get("sub")
        if subject is not None and (not isinstance(subject, str) or not subject or len(subject) > 1024):
            raise AuthenticationError("Invalid back-channel logout token")
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if (
            type(issued_at) is not int
            or type(expires_at) is not int
            or expires_at <= issued_at
            or expires_at - issued_at > 600
            or int(time.time()) - issued_at > 600
        ):
            raise AuthenticationError("Invalid back-channel logout token")
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
        if not isinstance(token, str) or not 1 <= len(token) <= 16_384:
            return None
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
        if not self._has_pinned_unverified_issuer(token):
            return None

        # Availability failure is not an invalid credential. Propagate the
        # bounded 502-class AKBError so browser refresh rolls its transaction
        # back instead of deleting a still-valid local session as if the token
        # had failed cryptographic verification.
        key = await self._resolve_signing_key(kid)
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
        # A client an operator named as the non-human service authority is
        # never a human client, on either route, even if a misconfiguration
        # also lists it among the human clients. The canonical loader refuses
        # that combination; this is the runtime half of the same refusal.
        service_admin_client_id = settings.keycloak_service_admin_client_id.strip()
        if service_admin_client_id and claims["azp"] == service_admin_client_id:
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

    # ── Non-human service-authority verification ─────────────────────
    async def verify_service_authority_token(self, token: str) -> VerifiedPrincipal | None:
        """Verify the one configured non-human administrative credential.

        This is a separate profile, not a relaxation of the human one. Keycloak
        issues a client-credentials token with no ``aud``, no ``sid``, and an
        empty ``scope``, so admitting it through ``keycloak-access-v1`` would
        mean dropping three requirements every human bearer must keep. The
        authority is instead pinned to what such a grant does establish: the
        realm that signed the token and the exact client that obtained it.

        ``keycloak_service_admin_client_id`` is what names that client. Blank
        keeps the whole profile inert.
        """
        service_admin_client_id = settings.keycloak_service_admin_client_id_effective
        if not service_admin_client_id:
            return None
        if not isinstance(token, str) or not 1 <= len(token) <= 16_384:
            return None
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as e:
            logger.debug("Keycloak service-authority token: malformed header (%s)", e)
            return None

        if header.get("alg") != "RS256" or header.get("typ") != "JWT":
            return None
        if _TOKEN_SUPPLIED_KEY_HEADERS.intersection(header):
            return None
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return None
        if not self._has_pinned_unverified_issuer(token):
            return None

        key = await self._resolve_signing_key(kid)
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
                issuer=settings.keycloak_issuer,
                options={
                    "verify_aud": False,
                    "require": ["exp", "iat", "jti", "iss", "sub", "typ", "azp"],
                },
            )
        except (jwt.PyJWTError, TypeError, ValueError) as e:
            logger.debug("Keycloak service-authority token verification failed: %s", e)
            return None

        required_strings = ("iss", "sub", "jti", "typ", "azp")
        if any(not isinstance(claims.get(name), str) or not claims[name].strip() for name in required_strings):
            return None
        if claims["typ"] != "Bearer":
            return None
        if claims["azp"] != service_admin_client_id:
            return None
        if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int:
            return None
        if claims["exp"] <= claims["iat"]:
            return None
        # Keycloak's own machine marker, and it must name the same client that
        # obtained the token — an `azp` alone is not proof of the grant type.
        if claims.get("client_id") != service_admin_client_id:
            return None
        preferred_username = claims.get("preferred_username")
        if preferred_username is not None and preferred_username != f"service-account-{service_admin_client_id}":
            return None
        # No person is behind this principal, and nothing downstream may
        # project a profile claim onto an account from it.
        if not _HUMAN_PROFILE_CLAIMS.isdisjoint(claims):
            return None
        scope = claims.get("scope")
        if scope is not None and not isinstance(scope, str):
            return None
        # A client-credentials token carries no audience unless an operator
        # adds a mapper. Whatever it does carry must not be another AKB
        # route's credential.
        raw_audience = claims.get("aud")
        if raw_audience is not None:
            if isinstance(raw_audience, str):
                audiences = {raw_audience}
            elif isinstance(raw_audience, list) and all(isinstance(item, str) and item for item in raw_audience):
                audiences = set(raw_audience)
            else:
                return None
            mcp_audience = settings.mcp_oauth_audience_effective
            if mcp_audience and mcp_audience in audiences:
                return None

        return VerifiedPrincipal(
            profile_id=KEYCLOAK_SERVICE_AUTHORITY_V1,
            issuer=claims["iss"],
            subject=claims["sub"],
            credential_type="access_token",
            claims=claims,
            audience=None,
        )

    # ── Logout URL construction ──────────────────────────────────────
    def ordinary_logout_url(self, post_logout_redirect: str) -> str:
        """Build logout navigation without accepting custodied ID material."""
        params = {
            "client_id": settings.keycloak_client_id,
            "post_logout_redirect_uri": post_logout_redirect,
        }
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
