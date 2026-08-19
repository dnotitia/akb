"""Durable resolution of the one configured non-human administrative principal.

The verifier proves that a bearer was minted by the exact identity-provider
client an operator named as this workspace's service authority. This module
turns that proof into an AKB account, and it does so without touching the human
account path at any point: no ``external_identities`` row, no enrollment
policy, no profile claims, no email.

The binding lives in ``service_identities`` and is keyed by (issuer, client),
not by subject. A client that is deleted and recreated gets a new
service-account user in the realm; keying on the client is what makes that a
refreshed binding instead of a second administrator.
"""

from __future__ import annotations

import uuid

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import (
    AccountSuspendedError,
    ExternalIdentityConflictError,
    ValidationError,
)
from app.repositories.events_repo import emit_event
from app.services.role_sync import get_role_sync


# Shared with account_service's service-user provisioning: an unusable value in
# a NOT NULL column, never a hash of anything.
_SERVICE_SENTINEL_HASH = "!service-account:no-local-login!"

# RFC 2606 reserves `.invalid`, so the address this account carries in a NOT
# NULL, UNIQUE column can never collide with, or be mistaken for, a mailbox.
_SERVICE_EMAIL_DOMAIN = "service.invalid"


def _required(value: str, field: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValidationError(f"{field} is required")
    return normalized


def _assert_bindable(row) -> None:
    """Refuse a binding whose account has stopped being this kind of principal.

    Administrator status is deliberately not asserted here. Creating the
    account grants it; an operator who later demotes it has made a decision,
    and silently re-granting on the next token would erase that decision. The
    demoted principal keeps authenticating and is refused by the routes that
    require an administrator.
    """
    if row["account_status"] != "active":
        raise AccountSuspendedError()
    if (
        row["account_kind"] != "service"
        or row["auth_provider"] != "service"
        or row["is_recovery_admin"]
        or row["has_external_identity"]
    ):
        raise ExternalIdentityConflictError()


def _result(row, *, newly_bound: bool) -> dict:
    return {
        "user_id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "display_name": row["display_name"],
        "is_admin": row["is_admin"],
        "newly_bound": newly_bound,
    }


async def resolve_service_authority(*, issuer: str, client_id: str, subject: str) -> dict:
    """Resolve, or on first use create, the AKB account for one service authority."""
    issuer = _required(issuer, "issuer")
    client_id = _required(client_id, "client_id")
    subject = _required(subject, "subject")

    username = f"service-{client_id}"
    email = f"{username}@{_SERVICE_EMAIL_DOMAIN}".lower()
    display_name = f"{client_id} service authority"

    pool = await get_pool()
    new_user_id: uuid.UUID | None = None

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                # Serialize concurrent first-use for one authority so two
                # requests cannot race into two accounts before the unique
                # constraints see either.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"service-authority:{len(issuer)}:{issuer}{client_id}",
                )
                bound = await conn.fetchrow(
                    """
                    SELECT s.id AS service_identity_id,
                           u.id, u.username, u.email, u.display_name, u.is_admin,
                           u.is_recovery_admin, u.auth_provider, u.account_status,
                           u.account_kind,
                           EXISTS (
                               SELECT 1 FROM external_identities e
                                WHERE e.user_id = u.id
                           ) AS has_external_identity
                      FROM service_identities s
                      JOIN users u ON u.id = s.user_id
                     WHERE s.issuer = $1 AND s.client_id = $2
                       FOR UPDATE OF u
                    """,
                    issuer,
                    client_id,
                )
                if bound is not None:
                    _assert_bindable(bound)
                    await conn.execute(
                        """
                        UPDATE service_identities
                           SET subject = $2, last_seen_at = NOW()
                         WHERE id = $1
                        """,
                        bound["service_identity_id"],
                        subject,
                    )
                    user_id = bound["id"]
                else:
                    user_id = uuid.uuid4()
                    await conn.execute(
                        """
                        INSERT INTO users (
                            id, username, email, password_hash, display_name,
                            is_admin, auth_provider, account_status, account_kind
                        ) VALUES ($1, $2, $3, $4, $5, true, 'service',
                                  'active', 'service')
                        """,
                        user_id,
                        username,
                        email,
                        _SERVICE_SENTINEL_HASH,
                        display_name,
                    )
                    await conn.execute(
                        """
                        INSERT INTO service_identities (
                            user_id, issuer, client_id, subject
                        ) VALUES ($1, $2, $3, $4)
                        """,
                        user_id,
                        issuer,
                        client_id,
                        subject,
                    )
                    await emit_event(
                        conn,
                        "auth.service_authority_bound",
                        actor_id=str(user_id),
                        payload={"issuer": issuer, "client_id": client_id},
                    )
                    new_user_id = user_id
        except asyncpg.UniqueViolationError:
            # A username, email, or identity collision with something this
            # authority does not own needs explicit administrative resolution;
            # it is never silently adopted.
            raise ExternalIdentityConflictError() from None

        row = await conn.fetchrow(
            """
            SELECT id, username, email, display_name, is_admin
              FROM users WHERE id = $1
            """,
            user_id,
        )

    if new_user_id is not None:
        await get_role_sync().on_user_create(new_user_id)
    return _result(row, newly_bound=new_user_id is not None)
