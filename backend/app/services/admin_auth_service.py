"""Product-admin authentication policy kept separate from ordinary login.

The SSO path stores only hashes of short-lived AKB-owned opaque values. It
never persists or returns Keycloak access, refresh, or ID tokens.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import (
    AccountSuspendedError,
    AuthenticationError,
    ExternalIdentityConflictError,
    ForbiddenError,
    MembershipRequiredError,
)
from app.repositories.events_repo import emit_event
from app.services.auth_service import (
    AuthenticatedUser,
    login,
    resolve_delegated_human_authorization,
)


@dataclass(frozen=True, slots=True)
class ProductAdminIdentity:
    user_id: uuid.UUID
    external_identity_id: uuid.UUID
    username: str
    email: str
    display_name: str | None
    auth_method: str


@dataclass(frozen=True, slots=True)
class IssuedAdminBrowserSession:
    token: str
    csrf_token: str
    expires_at: datetime


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


def _required_claim(
    claims: Mapping[str, object],
    name: str,
    *,
    max_length: int = 1024,
) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise AuthenticationError("Invalid admin identity token")
    return value


def _identity_from_row(row) -> ProductAdminIdentity:
    return ProductAdminIdentity(
        user_id=uuid.UUID(str(row["user_id"])),
        external_identity_id=uuid.UUID(str(row["external_identity_id"])),
        username=row["username"],
        email=row["email"] or "",
        display_name=row["display_name"],
        auth_method="keycloak",
    )


async def authenticate_local_product_admin(username: str, password: str) -> dict:
    """Authenticate through local authority and require AKB admin status."""
    result = await login(username, password)
    user = result.get("user")
    if not isinstance(user, dict) or user.get("is_admin") is not True:
        raise ForbiddenError("Product administrator access is required")
    return result


async def resolve_local_product_admin(authorization: str) -> AuthenticatedUser:
    user = await resolve_delegated_human_authorization(authorization)
    if user is None:
        raise AuthenticationError()
    if not user.is_admin:
        raise ForbiddenError("Product administrator access is required")
    return user


async def resolve_prebound_sso_product_admin(
    claims: Mapping[str, object],
) -> ProductAdminIdentity:
    """Resolve an exact binding; never JIT-create or email-adopt an account."""
    issuer = _required_claim(claims, "iss")
    subject = _required_claim(claims, "sub")
    if issuer != settings.keycloak_issuer:
        raise AuthenticationError("Invalid admin identity token")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT e.id AS external_identity_id, e.user_id,
                       u.username, u.email, u.display_name, u.is_admin,
                       u.auth_provider, u.account_status, u.account_kind
                  FROM external_identities e
                  JOIN users u ON u.id = e.user_id
                 WHERE e.issuer = $1 AND e.subject = $2
                 FOR UPDATE OF e
                """,
                issuer,
                subject,
            )
            if row is None:
                raise MembershipRequiredError()
            if row["account_status"] != "active":
                raise AccountSuspendedError()
            if row["account_kind"] != "human" or row["auth_provider"] != "keycloak":
                raise ExternalIdentityConflictError()
            if row["is_admin"] is not True:
                raise ForbiddenError("Product administrator access is required")
            await conn.execute(
                "UPDATE external_identities SET last_seen_at = NOW() WHERE id = $1",
                row["external_identity_id"],
            )
    return _identity_from_row(row)


async def create_sso_admin_browser_session(
    identity: ProductAdminIdentity,
    claims: Mapping[str, object],
) -> IssuedAdminBrowserSession:
    issuer = _required_claim(claims, "iss", max_length=2048)
    subject = _required_claim(claims, "sub")
    if issuer != settings.keycloak_issuer:
        raise AuthenticationError("Invalid admin identity token")
    sid = _required_claim(claims, "sid", max_length=255)
    raw_expiry = claims.get("exp")
    if type(raw_expiry) is not int:
        raise AuthenticationError("Invalid admin identity token")

    now = datetime.now(timezone.utc)
    try:
        id_token_expiry = datetime.fromtimestamp(raw_expiry, timezone.utc)
    except OverflowError, OSError, ValueError:
        raise AuthenticationError("Invalid admin identity token") from None
    configured_expiry = now + timedelta(seconds=settings.admin_browser_session_ttl_secs)
    expires_at = min(id_token_expiry, configured_expiry)
    if expires_at <= now:
        raise AuthenticationError("Invalid admin identity token")

    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    token_hash = _hash_credential(token)
    csrf_hash = _hash_credential(csrf_token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"admin-browser-session:{identity.user_id}",
            )
            exact_binding_is_live = await conn.fetchval(
                """
                SELECT TRUE
                  FROM external_identities e
                  JOIN users u ON u.id = e.user_id
                 WHERE e.id = $1
                   AND e.user_id = $2
                   AND e.issuer = $3
                   AND e.subject = $4
                   AND u.is_admin
                   AND u.account_status = 'active'
                   AND u.account_kind = 'human'
                   AND u.auth_provider = 'keycloak'
                 FOR SHARE OF e, u
                """,
                identity.external_identity_id,
                identity.user_id,
                issuer,
                subject,
            )
            if exact_binding_is_live is not True:
                raise AuthenticationError("Invalid admin identity token")
            await conn.execute("DELETE FROM admin_browser_sessions WHERE expires_at <= NOW()")
            # Retain at most seven existing sessions before adding this one.
            # This keeps retried IdP flows from growing the table without
            # bound while allowing a small number of admin devices.
            await conn.execute(
                """
                DELETE FROM admin_browser_sessions
                 WHERE id IN (
                    SELECT id
                      FROM admin_browser_sessions
                     WHERE user_id = $1
                     ORDER BY created_at DESC, id DESC
                    OFFSET 7
                 )
                """,
                identity.user_id,
            )
            await conn.execute(
                """
                INSERT INTO admin_browser_sessions (
                    token_hash, csrf_token_hash, user_id,
                    external_identity_id, identity_issuer, identity_subject,
                    keycloak_sid, expires_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                token_hash,
                csrf_hash,
                identity.user_id,
                identity.external_identity_id,
                issuer,
                subject,
                sid,
                expires_at,
            )
            await emit_event(
                conn,
                "auth.admin_session_created",
                actor_id=str(identity.user_id),
                payload={"auth_method": "keycloak"},
            )
    return IssuedAdminBrowserSession(
        token=token,
        csrf_token=csrf_token,
        expires_at=expires_at,
    )


async def resolve_sso_admin_browser_session(
    raw_token: str,
) -> ProductAdminIdentity:
    token = _bounded_credential(raw_token)
    if token is None:
        raise AuthenticationError()
    token_hash = _hash_credential(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT s.id AS session_id, s.user_id, s.external_identity_id,
                       u.username, u.email, u.display_name
                  FROM admin_browser_sessions s
                  JOIN users u ON u.id = s.user_id
                  JOIN external_identities e
                    ON e.id = s.external_identity_id AND e.user_id = s.user_id
                 WHERE s.token_hash = $1
                   AND s.expires_at > NOW()
                   AND s.identity_issuer = $2
                   AND e.issuer = s.identity_issuer
                   AND e.subject = s.identity_subject
                   AND u.is_admin
                   AND u.account_status = 'active'
                   AND u.account_kind = 'human'
                   AND u.auth_provider = 'keycloak'
                 FOR UPDATE OF s
                """,
                token_hash,
                settings.keycloak_issuer,
            )
            if row is None:
                await conn.execute(
                    "DELETE FROM admin_browser_sessions WHERE token_hash = $1",
                    token_hash,
                )
            else:
                await conn.execute(
                    "UPDATE admin_browser_sessions SET last_seen_at = NOW() WHERE id = $1",
                    row["session_id"],
                )
    if row is None:
        # Raise only after the cleanup transaction commits. Raising from inside
        # the transaction would roll back deletion of an expired/demoted row.
        raise AuthenticationError()
    return _identity_from_row(row)


async def validate_sso_admin_browser_session_csrf(
    raw_token: str,
    csrf_cookie: str,
    csrf_header: str,
) -> ProductAdminIdentity:
    """Resolve one live SSO admin session and its double-submit CSRF proof.

    The token, CSRF hash, exact external binding, and current AKB admin status
    are checked in one database lookup so a demotion or identity rebind takes
    effect before the control-plane mutation can start.
    """
    token = _bounded_credential(raw_token)
    cookie = _bounded_credential(csrf_cookie)
    header = _bounded_credential(csrf_header)
    if (
        token is None
        or cookie is None
        or header is None
        or not secrets.compare_digest(cookie, header)
    ):
        raise AuthenticationError("Invalid admin CSRF token")

    token_hash = _hash_credential(token)
    csrf_hash = _hash_credential(cookie)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT s.id AS session_id, s.csrf_token_hash,
                       s.user_id, s.external_identity_id,
                       u.username, u.email, u.display_name
                  FROM admin_browser_sessions s
                  JOIN users u ON u.id = s.user_id
                  JOIN external_identities e
                    ON e.id = s.external_identity_id AND e.user_id = s.user_id
                 WHERE s.token_hash = $1
                   AND s.expires_at > NOW()
                   AND s.identity_issuer = $2
                   AND e.issuer = s.identity_issuer
                   AND e.subject = s.identity_subject
                   AND u.is_admin
                   AND u.account_status = 'active'
                   AND u.account_kind = 'human'
                   AND u.auth_provider = 'keycloak'
                 FOR UPDATE OF s
                """,
                token_hash,
                settings.keycloak_issuer,
            )
            if (
                row is None
                or not isinstance(row["csrf_token_hash"], str)
                or not secrets.compare_digest(row["csrf_token_hash"], csrf_hash)
            ):
                raise AuthenticationError("Invalid admin CSRF token")
            await conn.execute(
                "UPDATE admin_browser_sessions SET last_seen_at = NOW() WHERE id = $1",
                row["session_id"],
            )
    return _identity_from_row(row)


async def revoke_sso_admin_browser_session(
    raw_token: str,
    csrf_cookie: str,
    csrf_header: str,
) -> ProductAdminIdentity:
    token = _bounded_credential(raw_token)
    cookie = _bounded_credential(csrf_cookie)
    header = _bounded_credential(csrf_header)
    if token is None or cookie is None or header is None or not secrets.compare_digest(cookie, header):
        raise AuthenticationError("Invalid admin CSRF token")

    token_hash = _hash_credential(token)
    csrf_hash = _hash_credential(cookie)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT s.id AS session_id, s.csrf_token_hash,
                       s.user_id, s.external_identity_id,
                       u.username, u.email, u.display_name
                  FROM admin_browser_sessions s
                  JOIN users u ON u.id = s.user_id
                 WHERE s.token_hash = $1 AND s.expires_at > NOW()
                 FOR UPDATE OF s
                """,
                token_hash,
            )
            if row is None or not secrets.compare_digest(row["csrf_token_hash"], csrf_hash):
                raise AuthenticationError("Invalid admin CSRF token")
            await conn.execute(
                "DELETE FROM admin_browser_sessions WHERE id = $1",
                row["session_id"],
            )
            await emit_event(
                conn,
                "auth.admin_session_revoked",
                actor_id=str(row["user_id"]),
                payload={"auth_method": "keycloak"},
            )
    return _identity_from_row(row)
