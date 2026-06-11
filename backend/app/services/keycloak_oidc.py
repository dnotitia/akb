"""Keycloak OIDC client — the OPTIONAL external-IdP login path.

This module is *only* exercised when ``settings.keycloak_enabled`` is
true. With it off, nothing here runs and AKB authenticates exactly as
before (local username/password + PAT).

Design (see docs/designs/keycloak-oidc/00-overview.md):

- Keycloak authenticates the person at the front door using the OIDC
  **authorization-code** flow (+ PKCE S256 for public clients).
- The ID token is verified **locally** against Keycloak's JWKS (RS256,
  cached, refetched once on key rotation) — no remote introspection on
  the hot path.
- Keycloak is **authentication only**. The caller maps the verified
  identity (by email) to an internal AKB user and mints a normal AKB
  JWT; the internal user model, PG-native RBAC, and PATs are untouched.

Implemented on the dependencies AKB already ships — ``httpx`` for the
token/JWKS HTTP calls and ``pyjwt`` for RS256 verification. No new
third-party packages.

Transient flow state (CSRF ``state`` + PKCE verifier, and the one-time
exchange codes) is persisted in the ``oidc_transients`` table so the
redirect round-trip works across multiple backend replicas. Each entry
is single-use and TTL-bounded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, AuthenticationError

logger = logging.getLogger("akb.keycloak")

# OIDC scopes requested at the authorization endpoint. `openid` is
# mandatory for an ID token; `profile`+`email` populate name/email claims
# we map onto the AKB user.
_SCOPE = "openid profile email"


# ── Transient store (oidc_transients table) ──────────────────────────
#
# Two single-use, TTL-bounded record kinds backing the redirect flow.
# Consume is an atomic DELETE … RETURNING so a code can never be redeemed
# twice even under concurrent callbacks.


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
    """Stateless-per-process OIDC client. All flow state lives in PG."""

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
    async def begin_login(self, redirect_path: str = "/") -> str:
        """Create CSRF state (+PKCE for public clients) and return the
        Keycloak authorization URL to redirect the browser to.

        ``redirect_path`` is the caller-vetted post-login destination carried
        opaquely through flow state. It is normally a same-site path, but may
        be an allowlisted companion-app absolute URL (cross-origin SSO); the
        callback (`_post_login_target`) re-validates it before delivering the
        one-time code, so this layer just stores it verbatim."""
        state = secrets.token_urlsafe(32)
        payload: dict[str, str] = {"redirect_path": redirect_path}
        params: dict[str, str] = {
            "client_id": settings.keycloak_client_id,
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

    # ── Token exchange ───────────────────────────────────────────────
    async def exchange_code_for_tokens(
        self, code: str, code_verifier: str | None
    ) -> dict[str, Any]:
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.keycloak_redirect_uri,
            "client_id": settings.keycloak_client_id,
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

    # ── JWKS + ID-token verification ─────────────────────────────────
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
        return next(
            (k for k in jwks.get("keys", []) if k.get("kid") == kid), None
        )

    async def verify_id_token(self, id_token: str) -> dict[str, Any]:
        """Verify a Keycloak ID token locally and return its claims.

        Validates signature (RS256), audience (client_id), issuer (realm),
        and expiry. Refetches JWKS once if the token's ``kid`` is unknown
        (key rotation) before giving up.
        """
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as e:
            raise AuthenticationError(f"Malformed ID token: {e}") from e

        kid = header.get("kid")
        if not kid:
            raise AuthenticationError("ID token missing kid header")

        jwks = await self._fetch_jwks()
        key = self._find_key(jwks, kid)
        if key is None:
            # Unknown kid — Keycloak likely rotated keys. Refetch once.
            jwks = await self._fetch_jwks(force=True)
            key = self._find_key(jwks, kid)
        if key is None:
            raise AuthenticationError("No matching Keycloak public key for token")

        try:
            public_key = RSAAlgorithm.from_jwk(json.dumps(key))
            claims = jwt.decode(
                id_token,
                public_key,
                algorithms=["RS256"],
                audience=settings.keycloak_client_id,
                issuer=settings.keycloak_issuer,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.InvalidTokenError as e:
            logger.warning("Keycloak ID token verification failed: %s", e)
            raise AuthenticationError(f"Invalid ID token: {e}") from e

        return claims

    # ── Logout URL ───────────────────────────────────────────────────
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


# ── One-time exchange code (callback → SPA) ──────────────────────────


async def issue_exchange_code(login_response: dict) -> str:
    """Store the freshly minted AKB JWT + user payload under a one-time
    opaque code and return the code. The SPA trades it via /exchange so
    the token is delivered in a POST body, never in a redirect URL."""
    code = secrets.token_urlsafe(32)
    await _store_issue(
        code, "exchange", login_response,
        ttl_secs=settings.keycloak_exchange_code_ttl_secs,
    )
    return code


async def redeem_exchange_code(code: str) -> dict | None:
    """Atomically redeem a one-time exchange code → {token, user} or None."""
    return await _store_consume(code, "exchange")
