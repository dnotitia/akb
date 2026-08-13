"""Server-side authority for ordinary Keycloak browser sessions.

The browser carries only random AKB-owned session and CSRF values. Keycloak
refresh and ID tokens remain encrypted in PostgreSQL; access tokens are
verified on issue/refresh and then discarded. Every request rechecks the exact
external identity and current AKB account state before existing authorization
continues.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, AuthenticationError, ForbiddenError
from app.repositories.events_repo import emit_event
from app.services.auth_service import AuthenticatedUser
from app.services.auth_verifier_profiles import KEYCLOAK_ACCESS_V1, VerifiedPrincipal
from app.services.keycloak_oidc import get_keycloak_oidc
from app.services.sso_browser_session_crypto import (
    BrowserSessionCipher,
    BrowserSessionKeyError,
    BrowserSessionPayloadError,
)
from app.sso.providers.keycloak_oidc import ProviderDefinitionError, validate_alias


@dataclass(frozen=True, slots=True)
class IssuedSsoBrowserSession:
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RevokedSsoBrowserSession:
    refresh_token: str | None


SSO_BROWSER_SESSION_COOKIE = "__Host-akb_sso_session"
SSO_BROWSER_CSRF_COOKIE = "__Host-akb_sso_csrf"
SSO_BROWSER_SESSION_COOKIE_DEV = "akb_dev_sso_session"
SSO_BROWSER_CSRF_COOKIE_DEV = "akb_dev_sso_csrf"
SSO_BROWSER_CSRF_HEADER = "X-AKB-CSRF"
_LOGOUT_FENCE_TTL = timedelta(minutes=15)
# Keep Keycloak stalls from occupying the entire default asyncpg pool. A
# non-locking indexed probe routes near-expiry requests here before they can
# wait on another refresh's row lock, then rechecks under FOR UPDATE after
# admission.
_BROWSER_REFRESH_CONCURRENCY = 4
_browser_refresh_gate = asyncio.Semaphore(_BROWSER_REFRESH_CONCURRENCY)


def sso_browser_session_cookie_name() -> str:
    """Return a host-locked production name or an isolated loopback name."""
    if urlsplit(settings.public_base_url).scheme == "https":
        return SSO_BROWSER_SESSION_COOKIE
    return SSO_BROWSER_SESSION_COOKIE_DEV


def sso_browser_csrf_cookie_name() -> str:
    """Keep the readable CSRF proof under the same host-only boundary."""
    if urlsplit(settings.public_base_url).scheme == "https":
        return SSO_BROWSER_CSRF_COOKIE
    return SSO_BROWSER_CSRF_COOKIE_DEV


def _hash_credential(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_credential(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not 20 <= len(value) <= 512
        or not value.isascii()
        or not all(character.isalnum() or character in "-_" for character in value)
    ):
        return None
    return value


def _bounded_token(value: object, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16_384
        or not value.isascii()
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        raise AuthenticationError("Invalid SSO token response")
    return value


def _required_claim(
    claims: Mapping[str, object],
    name: str,
    *,
    maximum: int,
) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AuthenticationError("Invalid SSO token response")
    return value


def _claim_expiry(claims: Mapping[str, object], now: datetime) -> datetime:
    raw_expiry = claims.get("exp")
    if type(raw_expiry) is not int:
        raise AuthenticationError("Invalid SSO token response")
    try:
        expiry = datetime.fromtimestamp(raw_expiry, timezone.utc)
    except OverflowError, OSError, ValueError:
        raise AuthenticationError("Invalid SSO token response") from None
    if expiry <= now:
        raise AuthenticationError("Invalid SSO token response")
    return expiry


def _claim_issued_at(claims: Mapping[str, object]) -> datetime:
    raw_issued_at = claims.get("iat")
    if type(raw_issued_at) is not int:
        raise AuthenticationError("Invalid SSO token response")
    try:
        return datetime.fromtimestamp(raw_issued_at, timezone.utc)
    except OverflowError, OSError, ValueError:
        raise AuthenticationError("Invalid SSO token response") from None


def _refresh_expiry(
    token_response: Mapping[str, object],
    *,
    now: datetime,
    absolute_expiry: datetime,
) -> datetime:
    lifetime = token_response.get("refresh_expires_in")
    if type(lifetime) is not int or not 1 <= lifetime <= 30 * 24 * 60 * 60:
        raise AuthenticationError("Invalid SSO token response")
    expiry = min(now + timedelta(seconds=lifetime), absolute_expiry)
    if expiry <= now:
        raise AuthenticationError("Invalid SSO token response")
    return expiry


def _session_context(session_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"session:{session_id}:user:{user_id}"


def _cipher() -> BrowserSessionCipher:
    try:
        return BrowserSessionCipher.from_encoded_key(settings.sso_browser_session_encryption_key)
    except BrowserSessionKeyError:
        raise AuthenticationError("SSO browser session is unavailable") from None


def _scope(claims: Mapping[str, object]) -> str:
    scope = _required_claim(claims, "scope", maximum=2048)
    values = [value for value in scope.split(" ") if value]
    if not values or any(len(value) > 255 for value in values):
        raise AuthenticationError("Invalid SSO token response")
    return " ".join(values)


def _provider_alias(claims: Mapping[str, object]) -> str:
    value = _required_claim(claims, "identity_provider", maximum=63)
    try:
        return validate_alias(value)
    except ProviderDefinitionError:
        raise AuthenticationError("Invalid SSO token response") from None


def _authenticated_user(row, scope: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(row["user_id"]),
        username=row["username"],
        email=row["email"] or "",
        display_name=row["display_name"],
        is_admin=row["is_admin"],
        auth_method="browser_session",
        oauth_scopes=scope.split(" "),
    )


def _exact_session_is_live(row) -> bool:
    return bool(
        row["identity_issuer"] == settings.keycloak_issuer
        and row["current_issuer"] == row["identity_issuer"]
        and row["current_subject"] == row["identity_subject"]
        and row["account_status"] == "active"
        and row["account_kind"] == "human"
        and row["auth_provider"] == "keycloak"
    )


async def create_sso_browser_session(
    user: AuthenticatedUser,
    principal: VerifiedPrincipal,
    id_claims: Mapping[str, object],
    token_response: Mapping[str, object],
) -> IssuedSsoBrowserSession:
    """Persist one exact-bound session after both token profiles verified."""
    if principal.profile_id != KEYCLOAK_ACCESS_V1 or user.auth_method != "oauth":
        raise AuthenticationError("Invalid SSO token response")
    issuer = _required_claim(principal.claims, "iss", maximum=2048)
    subject = _required_claim(principal.claims, "sub", maximum=1024)
    sid = _required_claim(principal.claims, "sid", maximum=255)
    provider_alias = _provider_alias(principal.claims)
    if (
        (issuer, subject) != (principal.issuer, principal.subject)
        or issuer != settings.keycloak_issuer
        or id_claims.get("iss") != issuer
        or id_claims.get("sub") != subject
        or id_claims.get("sid") != sid
        or _provider_alias(id_claims) != provider_alias
    ):
        raise AuthenticationError("Invalid SSO token response")

    access_token = _bounded_token(token_response.get("access_token"))
    refresh_token = _bounded_token(token_response.get("refresh_token"))
    id_token = _bounded_token(token_response.get("id_token"))
    if token_response.get("token_type") != "Bearer":
        raise AuthenticationError("Invalid SSO token response")
    assert access_token is not None
    assert refresh_token is not None
    assert id_token is not None

    now = datetime.now(timezone.utc)
    session_issued_at = _claim_issued_at(principal.claims)
    access_expiry = _claim_expiry(principal.claims, now)
    absolute_expiry = now + timedelta(seconds=settings.sso_browser_session_absolute_ttl_secs)
    idle_expiry = min(
        now + timedelta(seconds=settings.sso_browser_session_idle_ttl_secs),
        absolute_expiry,
    )
    refresh_expiry = _refresh_expiry(
        token_response,
        now=now,
        absolute_expiry=absolute_expiry,
    )
    scope = _scope(principal.claims)

    try:
        user_id = uuid.UUID(user.user_id)
    except AttributeError, TypeError, ValueError:
        raise AuthenticationError("Invalid SSO token response") from None
    session_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    envelope = _cipher().seal(
        {
            "refresh_token": refresh_token,
            "id_token": id_token,
            "scope": scope,
            "provider_alias": provider_alias,
        },
        context=_session_context(session_id, user_id),
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"sso-browser-session:{user_id}",
            )
            # Session creation and back-channel logout share this exact lock.
            # The durable fence below then rejects a callback that resumes
            # after a verified logout event has already committed.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"sso-browser-sid:{issuer}:{sid}",
            )
            await conn.execute("DELETE FROM sso_browser_logout_fences WHERE expires_at <= NOW()")
            fenced = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM sso_browser_logout_fences
                     WHERE identity_issuer = $1
                       AND keycloak_sid = $2
                       AND (identity_subject IS NULL OR identity_subject = $3)
                       AND logout_issued_at >= $4
                       AND expires_at > NOW()
                )
                """,
                issuer,
                sid,
                subject,
                session_issued_at,
            )
            if fenced:
                raise AuthenticationError("SSO session was logged out")
            identity = await conn.fetchrow(
                """
                SELECT e.id
                  FROM external_identities e
                  JOIN users u ON u.id = e.user_id
                 WHERE e.user_id = $1
                   AND e.issuer = $2
                   AND e.subject = $3
                   AND u.account_status = 'active'
                   AND u.account_kind = 'human'
                   AND u.auth_provider = 'keycloak'
                 FOR SHARE OF e, u
                """,
                user_id,
                issuer,
                subject,
            )
            if identity is None:
                raise AuthenticationError("Invalid SSO token response")
            await conn.execute(
                """
                DELETE FROM sso_browser_sessions
                 WHERE idle_expires_at <= NOW()
                    OR absolute_expires_at <= NOW()
                    OR refresh_expires_at <= NOW()
                """
            )
            # Keep at most eight active browser handles per AKB account. Old
            # ciphertext is deleted before the replacement is committed, so
            # no evicted refresh credential remains available to AKB.
            await conn.execute(
                """
                DELETE FROM sso_browser_sessions
                 WHERE id IN (
                    SELECT id
                      FROM sso_browser_sessions
                     WHERE user_id = $1
                     ORDER BY created_at DESC, id DESC
                    OFFSET 7
                 )
                """,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO sso_browser_sessions (
                    id, token_hash, csrf_token_hash, user_id,
                    external_identity_id, identity_issuer, identity_subject,
                    keycloak_sid, token_envelope, access_expires_at,
                    refresh_expires_at, idle_expires_at, absolute_expires_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
                )
                """,
                session_id,
                _hash_credential(token),
                _hash_credential(csrf_token),
                user_id,
                identity["id"],
                issuer,
                subject,
                sid,
                envelope,
                access_expiry,
                refresh_expiry,
                idle_expiry,
                absolute_expiry,
            )
            await emit_event(
                conn,
                "auth.sso_browser_session_created",
                actor_id=str(user_id),
                payload={"auth_method": "keycloak"},
            )
    # `access_token` is deliberately not returned or persisted.
    return IssuedSsoBrowserSession(
        token=token,
        csrf_token=csrf_token,
        expires_at=absolute_expiry,
    )


async def resolve_sso_browser_session(
    raw_token: str,
    *,
    require_csrf: bool = False,
    csrf_cookie: str = "",
    csrf_header: str = "",
) -> AuthenticatedUser:
    """Resolve and, when needed, rotate one exact-bound browser session."""
    token = _bounded_credential(raw_token)
    if token is None:
        raise AuthenticationError()
    csrf_hash: str | None = None
    if require_csrf:
        cookie = _bounded_credential(csrf_cookie)
        header = _bounded_credential(csrf_header)
        if cookie is None or header is None or not secrets.compare_digest(cookie, header):
            raise ForbiddenError("Invalid SSO CSRF token")
        csrf_hash = _hash_credential(cookie)

    token_hash = _hash_credential(token)
    result: AuthenticatedUser | None = None
    needs_refresh = await _sso_browser_session_needs_refresh(token_hash)
    if not needs_refresh:
        result, needs_refresh = await _resolve_sso_browser_session_pass(
            token_hash,
            require_csrf=require_csrf,
            csrf_hash=csrf_hash,
            allow_refresh=False,
        )
    if needs_refresh:
        # Wait without a PostgreSQL connection or row lock. Once admitted, the
        # second pass re-reads everything; another request may already have
        # completed this exact session's rotation while we were queued.
        async with _browser_refresh_gate:
            result, needs_refresh = await _resolve_sso_browser_session_pass(
                token_hash,
                require_csrf=require_csrf,
                csrf_hash=csrf_hash,
                allow_refresh=True,
            )
    if result is None or needs_refresh:
        raise AuthenticationError()
    return result


async def _sso_browser_session_needs_refresh(token_hash: str) -> bool:
    """Probe expiry without waiting on a concurrent refresh row lock."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        access_expires_at = await conn.fetchval(
            """
            SELECT access_expires_at
              FROM sso_browser_sessions
             WHERE token_hash = $1
            """,
            token_hash,
        )
    if not isinstance(access_expires_at, datetime):
        return False
    refresh_at = datetime.now(timezone.utc) + timedelta(seconds=settings.sso_browser_session_refresh_skew_secs)
    return access_expires_at <= refresh_at


async def _resolve_sso_browser_session_pass(
    token_hash: str,
    *,
    require_csrf: bool,
    csrf_hash: str | None,
    allow_refresh: bool,
) -> tuple[AuthenticatedUser | None, bool]:
    """Run one locked resolution pass, optionally performing remote refresh."""
    invalid = False
    result: AuthenticatedUser | None = None
    needs_refresh = False
    transient_error: AKBError | None = None
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT s.*, e.issuer AS current_issuer,
                       e.subject AS current_subject,
                       u.username, u.email, u.display_name, u.is_admin,
                       u.auth_provider, u.account_status, u.account_kind
                  FROM sso_browser_sessions s
                  JOIN users u ON u.id = s.user_id
                  JOIN external_identities e
                    ON e.id = s.external_identity_id AND e.user_id = s.user_id
                 WHERE s.token_hash = $1
                 FOR UPDATE OF s
                """,
                token_hash,
            )
            now = datetime.now(timezone.utc)
            if row is None:
                invalid = True
            elif (
                not _exact_session_is_live(row)
                or row["idle_expires_at"] <= now
                or row["absolute_expires_at"] <= now
                or row["refresh_expires_at"] <= now
            ):
                await conn.execute(
                    "DELETE FROM sso_browser_sessions WHERE id = $1",
                    row["id"],
                )
                invalid = True
            elif require_csrf and (
                not isinstance(row["csrf_token_hash"], str)
                or csrf_hash is None
                or not secrets.compare_digest(row["csrf_token_hash"], csrf_hash)
            ):
                # A bad double-submit proof must not become a cross-site
                # logout primitive. Keep the valid session untouched.
                raise ForbiddenError("Invalid SSO CSRF token")
            else:
                refresh_at = now + timedelta(seconds=settings.sso_browser_session_refresh_skew_secs)
                if row["access_expires_at"] <= refresh_at and not allow_refresh:
                    needs_refresh = True
                else:
                    context = _session_context(row["id"], row["user_id"])
                    try:
                        custody = _cipher().open(
                            row["token_envelope"],
                            context=context,
                        )
                    except BrowserSessionPayloadError:
                        await conn.execute(
                            "DELETE FROM sso_browser_sessions WHERE id = $1",
                            row["id"],
                        )
                        invalid = True
                    else:
                        scope = custody["scope"]
                        if row["access_expires_at"] <= refresh_at:
                            try:
                                scope, transient_error = await _refresh_locked_session(
                                    conn,
                                    row,
                                    custody,
                                    context=context,
                                    now=now,
                                )
                            except AuthenticationError:
                                await conn.execute(
                                    "DELETE FROM sso_browser_sessions WHERE id = $1",
                                    row["id"],
                                )
                                invalid = True
                        if not invalid and transient_error is None:
                            idle_expiry = min(
                                now + timedelta(seconds=settings.sso_browser_session_idle_ttl_secs),
                                row["absolute_expires_at"],
                            )
                            await conn.execute(
                                """
                                UPDATE sso_browser_sessions
                                   SET last_seen_at = NOW(), idle_expires_at = $2
                                 WHERE id = $1
                                """,
                                row["id"],
                                idle_expiry,
                            )
                            result = _authenticated_user(row, scope)
    if transient_error is not None:
        # A rotated refresh credential, when present, was committed before the
        # transient verification error escapes to the caller.
        raise transient_error
    if invalid:
        raise AuthenticationError()
    return result, needs_refresh


async def _refresh_locked_session(
    conn,
    row,
    custody: Mapping[str, str],
    *,
    context: str,
    now: datetime,
) -> tuple[str, AKBError | None]:
    """Refresh while holding this session row lock to serialize rotation."""
    oidc = get_keycloak_oidc()
    token_response = await oidc.refresh_browser_tokens(custody["refresh_token"])
    if token_response.get("token_type") != "Bearer":
        raise AuthenticationError("SSO browser session refresh failed")
    access_token = _bounded_token(token_response.get("access_token"))
    refresh_token = _bounded_token(
        token_response.get("refresh_token"),
        required=False,
    )
    id_token = _bounded_token(token_response.get("id_token"), required=False)
    assert access_token is not None
    try:
        principal = await oidc.verify_access_token(
            access_token,
            settings.api_oauth_audience_effective,
            route_profile="api",
        )
        if principal is None:
            raise AuthenticationError("SSO browser session refresh failed")
        issuer = _required_claim(principal.claims, "iss", maximum=2048)
        subject = _required_claim(principal.claims, "sub", maximum=1024)
        sid = _required_claim(principal.claims, "sid", maximum=255)
        provider_alias = _provider_alias(principal.claims)
        if (
            principal.profile_id != KEYCLOAK_ACCESS_V1
            or (issuer, subject) != (principal.issuer, principal.subject)
            or issuer != row["identity_issuer"]
            or subject != row["identity_subject"]
            or sid != row["keycloak_sid"]
            or provider_alias != custody["provider_alias"]
        ):
            raise AuthenticationError("SSO browser session refresh failed")

        if id_token is not None:
            id_claims = await oidc.verify_id_token(
                id_token,
                client_id=settings.keycloak_client_id,
            )
            if (
                id_claims.get("iss") != issuer
                or id_claims.get("sub") != subject
                or id_claims.get("sid") != sid
                or id_claims.get("azp") != settings.keycloak_client_id
                or _provider_alias(id_claims) != custody["provider_alias"]
            ):
                raise AuthenticationError("SSO browser session refresh failed")
        else:
            id_token = custody["id_token"]
    except AuthenticationError:
        raise
    except AKBError as exc:
        if await _preserve_rotated_refresh_after_verification_outage(
            conn,
            row,
            custody,
            context=context,
            token_response=token_response,
            candidate_refresh_token=refresh_token,
            now=now,
        ):
            return custody["scope"], exc
        raise
    if refresh_token is None:
        refresh_token = custody["refresh_token"]

    scope = _scope(principal.claims)
    access_expiry = _claim_expiry(principal.claims, now)
    refresh_expiry = _refresh_expiry(
        token_response,
        now=now,
        absolute_expiry=row["absolute_expires_at"],
    )
    envelope = _cipher().seal(
        {
            "refresh_token": refresh_token,
            "id_token": id_token,
            "scope": scope,
            "provider_alias": custody["provider_alias"],
        },
        context=context,
    )
    await conn.execute(
        """
        UPDATE sso_browser_sessions
           SET token_envelope = $2,
               access_expires_at = $3,
               refresh_expires_at = $4,
               refreshed_at = NOW()
         WHERE id = $1
        """,
        row["id"],
        envelope,
        access_expiry,
        refresh_expiry,
    )
    return scope, None


async def _preserve_rotated_refresh_after_verification_outage(
    conn,
    row,
    custody: Mapping[str, str],
    *,
    context: str,
    token_response: Mapping[str, object],
    candidate_refresh_token: str | None,
    now: datetime,
) -> bool:
    """Commit only a rotated opaque credential when JWKS is unavailable.

    The new access/ID claims remain untrusted and are not adopted. Persisting
    the bounded opaque refresh value prevents transaction rollback from
    restoring a refresh token that Keycloak may already have invalidated.
    """
    if candidate_refresh_token is None or secrets.compare_digest(
        candidate_refresh_token,
        custody["refresh_token"],
    ):
        return False
    refresh_expiry = _refresh_expiry(
        token_response,
        now=now,
        absolute_expiry=row["absolute_expires_at"],
    )
    envelope = _cipher().seal(
        {
            "refresh_token": candidate_refresh_token,
            "id_token": custody["id_token"],
            "scope": custody["scope"],
            "provider_alias": custody["provider_alias"],
        },
        context=context,
    )
    retry_at = max(now, row["created_at"] + timedelta(microseconds=1))
    await conn.execute(
        """
        UPDATE sso_browser_sessions
           SET token_envelope = $2,
               access_expires_at = $3,
               refresh_expires_at = $4,
               refreshed_at = NOW()
         WHERE id = $1
        """,
        row["id"],
        envelope,
        retry_at,
        refresh_expiry,
    )
    return True


async def revoke_sso_browser_session(
    raw_token: str,
    csrf_cookie: str,
    csrf_header: str,
) -> RevokedSsoBrowserSession:
    """Delete one local handle and return its encrypted revocation material."""
    token = _bounded_credential(raw_token)
    cookie = _bounded_credential(csrf_cookie)
    header = _bounded_credential(csrf_header)
    if token is None or cookie is None or header is None or not secrets.compare_digest(cookie, header):
        raise ForbiddenError("Invalid SSO CSRF token")
    token_hash = _hash_credential(token)
    csrf_hash = _hash_credential(cookie)
    result: RevokedSsoBrowserSession | None = None
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, user_id, csrf_token_hash, token_envelope
                  FROM sso_browser_sessions
                 WHERE token_hash = $1
                 FOR UPDATE
                """,
                token_hash,
            )
            if (
                row is None
                or not isinstance(row["csrf_token_hash"], str)
                or not secrets.compare_digest(row["csrf_token_hash"], csrf_hash)
            ):
                raise ForbiddenError("Invalid SSO CSRF token")
            try:
                custody = _cipher().open(
                    row["token_envelope"],
                    context=_session_context(row["id"], row["user_id"]),
                )
                result = RevokedSsoBrowserSession(
                    refresh_token=custody["refresh_token"],
                )
            except BrowserSessionPayloadError:
                result = RevokedSsoBrowserSession(
                    refresh_token=None,
                )
            await conn.execute(
                "DELETE FROM sso_browser_sessions WHERE id = $1",
                row["id"],
            )
            await emit_event(
                conn,
                "auth.sso_browser_session_revoked",
                actor_id=str(row["user_id"]),
                payload={"auth_method": "keycloak"},
            )
    assert result is not None
    return result


async def revoke_sso_browser_sessions_from_logout_token(
    *,
    issuer: str,
    sid: str,
    subject: str | None,
    issued_at: int,
    expires_at: int,
) -> int:
    """Fence late callbacks and remove sessions selected by a logout token."""
    if (
        issuer != settings.keycloak_issuer
        or not sid
        or len(sid) > 255
        or (subject is not None and (not subject or len(subject) > 1024))
        or type(issued_at) is not int
        or type(expires_at) is not int
    ):
        raise AuthenticationError("Invalid back-channel logout token")
    try:
        event_issued_at = datetime.fromtimestamp(issued_at, timezone.utc)
        event_expires_at = datetime.fromtimestamp(expires_at, timezone.utc)
    except OverflowError, OSError, ValueError:
        raise AuthenticationError("Invalid back-channel logout token") from None
    now = datetime.now(timezone.utc)
    if event_expires_at <= event_issued_at or event_expires_at <= now:
        raise AuthenticationError("Invalid back-channel logout token")
    fence_expires_at = max(event_expires_at, now + _LOGOUT_FENCE_TTL)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"sso-browser-sid:{issuer}:{sid}",
            )
            await conn.execute("DELETE FROM sso_browser_logout_fences WHERE expires_at <= NOW()")
            await conn.execute(
                """
                INSERT INTO sso_browser_logout_fences (
                    identity_issuer, keycloak_sid, identity_subject,
                    logout_issued_at, expires_at
                ) VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (identity_issuer, keycloak_sid) DO UPDATE
                   SET identity_subject = CASE
                           WHEN EXCLUDED.logout_issued_at >=
                                sso_browser_logout_fences.logout_issued_at
                           THEN EXCLUDED.identity_subject
                           ELSE sso_browser_logout_fences.identity_subject
                       END,
                       logout_issued_at = GREATEST(
                           sso_browser_logout_fences.logout_issued_at,
                           EXCLUDED.logout_issued_at
                       ),
                       expires_at = GREATEST(
                           sso_browser_logout_fences.expires_at,
                           EXCLUDED.expires_at
                       ),
                       received_at = NOW()
                """,
                issuer,
                sid,
                subject,
                event_issued_at,
                fence_expires_at,
            )
            result = await conn.execute(
                """
                DELETE FROM sso_browser_sessions
                 WHERE identity_issuer = $1
                   AND keycloak_sid = $2
                   AND ($3::TEXT IS NULL OR identity_subject = $3)
                """,
                issuer,
                sid,
                subject,
            )
    try:
        return int(result.rsplit(" ", 1)[-1])
    except AttributeError, ValueError:
        return 0
