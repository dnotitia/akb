"""Explicit, non-HTTP provisioning for the designated recovery administrator."""

from __future__ import annotations

import uuid
from typing import Literal

import asyncpg

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import (
    RecoveryAdminConflictError,
    RecoveryAdminModeError,
    ValidationError,
)
from app.repositories.events_repo import emit_event
from app.services.auth_service import hash_password_async
from app.services.role_sync import get_role_sync


# Fixed signed-bigint key: stable across processes and PostgreSQL upgrades,
# unlike a runtime hash whose implementation could change.
_PROVISIONING_LOCK_ID = 0x414B425245434F56  # "AKBRECOV"
_SSO_PASSWORD_SENTINEL = "!keycloak-sso:no-local-login!"


def _required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field} is required")
    return normalized


def _email(value: str) -> str:
    return _required(value, "email").lower()


def _require_mode(expected: Literal["local", "sso"]) -> None:
    if settings.require_auth_mode() != expected:
        raise RecoveryAdminModeError()


def _validate_password(password: str) -> None:
    if len(password) < 12:
        raise ValidationError("password must be at least 12 characters")
    if len(password.encode("utf-8")) > 72:
        raise ValidationError("password must be at most 72 UTF-8 bytes")


async def _lock_provisioning(conn) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1::bigint)",
        _PROVISIONING_LOCK_ID,
    )


async def _designated_user(conn):
    return await conn.fetchrow(
        """
        SELECT id, username, email, password_hash, is_admin, is_recovery_admin,
               auth_provider, account_status, account_kind
          FROM users
         WHERE is_recovery_admin
         FOR UPDATE
        """
    )


def _result(row, *, mode: Literal["local", "sso"], created: bool) -> dict:
    return {
        "user_id": str(row["id"]),
        "username": row["username"],
        "email": row["email"],
        "auth_mode": mode,
        "created": created,
        "is_admin": bool(row["is_admin"]),
        "is_recovery_admin": bool(row["is_recovery_admin"]),
    }


async def _emit_provisioned(conn, user_id: uuid.UUID, mode: Literal["local", "sso"]) -> None:
    await emit_event(
        conn,
        "auth.recovery_admin_provisioned",
        actor_id=None,
        payload={"user_id": str(user_id), "auth_mode": mode},
    )


async def provision_local_recovery_admin(
    *,
    username: str,
    email: str,
    password: str,
) -> dict:
    """Create the one local recovery admin, or converge on its exact identity."""
    _require_mode("local")
    username = _required(username, "username")
    email = _email(email)
    _validate_password(password)
    password_hash = await hash_password_async(password)
    pool = await get_pool()
    created = False

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _lock_provisioning(conn)
                row = await _designated_user(conn)
                if row is not None:
                    identity_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM external_identities WHERE user_id = $1",
                        row["id"],
                    )
                    if not (
                        row["username"] == username
                        and row["email"].lower() == email
                        and row["is_admin"]
                        and row["is_recovery_admin"]
                        and row["auth_provider"] == "local"
                        and row["account_status"] == "active"
                        and row["account_kind"] == "human"
                        and identity_count == 0
                    ):
                        raise RecoveryAdminConflictError()
                else:
                    collision = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM users
                             WHERE username = $1 OR email = $2
                        )
                        """,
                        username,
                        email,
                    )
                    if collision:
                        raise RecoveryAdminConflictError()
                    user_id = uuid.uuid4()
                    row = await conn.fetchrow(
                        """
                        INSERT INTO users (
                            id, username, email, password_hash, is_admin,
                            is_recovery_admin, auth_provider, account_status, account_kind
                        ) VALUES ($1, $2, $3, $4, true, true, 'local', 'active', 'human')
                        RETURNING id, username, email, password_hash, is_admin,
                                  is_recovery_admin, auth_provider, account_status, account_kind
                        """,
                        user_id,
                        username,
                        email,
                        password_hash,
                    )
                    await _emit_provisioned(conn, user_id, "local")
                    created = True
    except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError):
        raise RecoveryAdminConflictError() from None

    if created:
        await get_role_sync().on_user_create(row["id"])
    return _result(row, mode="local", created=created)


async def provision_sso_recovery_admin(
    *,
    username: str,
    email: str,
    issuer: str,
    subject: str,
) -> dict:
    """Pre-bind the one SSO recovery admin to an exact issuer and subject."""
    _require_mode("sso")
    username = _required(username, "username")
    email = _email(email)
    issuer = _required(issuer, "issuer")
    subject = _required(subject, "subject")
    pool = await get_pool()
    created = False

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _lock_provisioning(conn)
                row = await _designated_user(conn)
                if row is not None:
                    identities = await conn.fetch(
                        """
                        SELECT issuer, subject, username_snapshot, email_snapshot
                          FROM external_identities
                         WHERE user_id = $1
                         ORDER BY issuer, subject
                        """,
                        row["id"],
                    )
                    exact_identity = len(identities) == 1 and (
                        identities[0]["issuer"] == issuer
                        and identities[0]["subject"] == subject
                        and identities[0]["username_snapshot"] == username
                        and (identities[0]["email_snapshot"] or "").lower() == email
                    )
                    if not (
                        row["username"] == username
                        and row["email"].lower() == email
                        and row["password_hash"] == _SSO_PASSWORD_SENTINEL
                        and row["is_admin"]
                        and row["is_recovery_admin"]
                        and row["auth_provider"] == "keycloak"
                        and row["account_status"] == "active"
                        and row["account_kind"] == "human"
                        and exact_identity
                    ):
                        raise RecoveryAdminConflictError()
                else:
                    user_collision = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM users
                             WHERE username = $1 OR email = $2
                        )
                        """,
                        username,
                        email,
                    )
                    identity_collision = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM external_identities
                             WHERE issuer = $1 AND subject = $2
                        )
                        """,
                        issuer,
                        subject,
                    )
                    if user_collision or identity_collision:
                        raise RecoveryAdminConflictError()
                    user_id = uuid.uuid4()
                    row = await conn.fetchrow(
                        """
                        INSERT INTO users (
                            id, username, email, password_hash, is_admin,
                            is_recovery_admin, auth_provider, account_status, account_kind
                        ) VALUES ($1, $2, $3, $4, true, true, 'keycloak', 'active', 'human')
                        RETURNING id, username, email, password_hash, is_admin,
                                  is_recovery_admin, auth_provider, account_status, account_kind
                        """,
                        user_id,
                        username,
                        email,
                        _SSO_PASSWORD_SENTINEL,
                    )
                    await conn.execute(
                        """
                        INSERT INTO external_identities (
                            user_id, issuer, subject, username_snapshot, email_snapshot
                        ) VALUES ($1, $2, $3, $4, $5)
                        """,
                        user_id,
                        issuer,
                        subject,
                        username,
                        email,
                    )
                    await _emit_provisioned(conn, user_id, "sso")
                    created = True
    except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError):
        raise RecoveryAdminConflictError() from None

    if created:
        await get_role_sync().on_user_create(row["id"])
    return _result(row, mode="sso", created=created)
