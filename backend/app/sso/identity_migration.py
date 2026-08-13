"""Exact external-identity continuity for an existing AKB account.

This module deliberately does less than ordinary account projection.  It does
not discover or adopt an account by email, mutate the user profile, revoke a
credential, or move a grant.  A caller must name both an existing AKB user and
that user's exact old ``(issuer, subject)`` binding.  The only successful
first-time mutation is insertion of one additional exact binding for the same
user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import uuid

from app.db.postgres import get_pool
from app.repositories.events_repo import emit_event


IdentityMigrationState = Literal["ready_to_link", "linked"]
_MAX_ISSUER_LENGTH = 2048
_MAX_SUBJECT_LENGTH = 1024


class IdentityMigrationError(RuntimeError):
    """Value-less migration rejection safe for API and audit diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class IdentityMigrationReadback:
    """Secret-free evidence for one exact identity-continuity decision."""

    user_id: uuid.UUID
    state: IdentityMigrationState
    old_issuer: str
    new_issuer: str
    # Internal mutation ownership.  This is deliberately omitted from the
    # admin response: callers need it only to decide whether this exact call
    # owns a compensating write after a cross-authority post-check fails.
    binding_changed: bool = False

    def admin_view(self) -> dict[str, str]:
        return {
            "user_id": str(self.user_id),
            "state": self.state,
            "old_issuer": self.old_issuer,
            "new_issuer": self.new_issuer,
        }


@dataclass(frozen=True, slots=True)
class _IdentitySnapshot:
    username: str | None
    email: str | None


def _user_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise IdentityMigrationError("identity_migration_user_id_invalid") from None


def _identity_part(value: str, *, kind: Literal["issuer", "subject"]) -> str:
    maximum = _MAX_ISSUER_LENGTH if kind == "issuer" else _MAX_SUBJECT_LENGTH
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise IdentityMigrationError(f"identity_migration_{kind}_invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise IdentityMigrationError(
            f"identity_migration_{kind}_invalid"
        ) from None
    # ``subject`` is an opaque, case-sensitive identifier.  Do not strip or
    # Unicode-normalize it; changing even one code point changes the identity.
    return value


def _actor(value: str) -> str:
    if not isinstance(value, str):
        raise IdentityMigrationError("identity_migration_actor_invalid")
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > 255
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in cleaned)
    ):
        raise IdentityMigrationError("identity_migration_actor_invalid")
    try:
        cleaned.encode("utf-8")
    except UnicodeEncodeError:
        raise IdentityMigrationError("identity_migration_actor_invalid") from None
    return cleaned


def _lock_key(issuer: str, subject: str) -> str:
    # Keep this byte-for-byte compatible with account_service's external
    # identity creation lock so migration and ordinary projection serialize.
    return f"external-identity:{len(issuer)}:{issuer}{subject}"


def _validated_request(
    *,
    existing_user_id: str,
    old_issuer: str,
    old_subject: str,
    new_issuer: str,
    new_subject: str,
) -> tuple[uuid.UUID, str, str, str, str]:
    user_id = _user_uuid(existing_user_id)
    old_issuer = _identity_part(old_issuer, kind="issuer")
    old_subject = _identity_part(old_subject, kind="subject")
    new_issuer = _identity_part(new_issuer, kind="issuer")
    new_subject = _identity_part(new_subject, kind="subject")
    if (old_issuer, old_subject) == (new_issuer, new_subject):
        raise IdentityMigrationError("identity_migration_binding_unchanged")
    return user_id, old_issuer, old_subject, new_issuer, new_subject


async def _inspect_in_conn(
    conn,
    *,
    user_id: uuid.UUID,
    old_issuer: str,
    old_subject: str,
    new_issuer: str,
    new_subject: str,
    lock_user: bool,
) -> tuple[IdentityMigrationReadback, _IdentitySnapshot]:
    lock_clause = " FOR UPDATE" if lock_user else ""
    user = await conn.fetchrow(
        """
        SELECT id, username, email, auth_provider, account_status, account_kind
          FROM users
         WHERE id = $1
        """
        + lock_clause,
        user_id,
    )
    if user is None:
        raise IdentityMigrationError("identity_migration_target_not_found")
    if user["account_status"] != "active":
        raise IdentityMigrationError("identity_migration_target_inactive")
    if user["account_kind"] != "human":
        raise IdentityMigrationError("identity_migration_target_not_human")
    if user["auth_provider"] != "keycloak":
        raise IdentityMigrationError("identity_migration_target_not_external")

    old_binding = await conn.fetchrow(
        """
        SELECT user_id, username_snapshot, email_snapshot
          FROM external_identities
         WHERE issuer = $1 AND subject = $2
        """
        + lock_clause,
        old_issuer,
        old_subject,
    )
    if old_binding is None or old_binding["user_id"] != user_id:
        raise IdentityMigrationError("identity_migration_old_binding_missing")

    new_owner = await conn.fetchval(
        """
        SELECT user_id
          FROM external_identities
         WHERE issuer = $1 AND subject = $2
        """
        + lock_clause,
        new_issuer,
        new_subject,
    )
    if new_owner is not None and new_owner != user_id:
        raise IdentityMigrationError("identity_migration_new_binding_conflict")

    state: IdentityMigrationState = (
        "linked" if new_owner == user_id else "ready_to_link"
    )
    return (
        IdentityMigrationReadback(
            user_id=user_id,
            state=state,
            old_issuer=old_issuer,
            new_issuer=new_issuer,
        ),
        _IdentitySnapshot(
            username=(
                old_binding["username_snapshot"]
                if isinstance(old_binding["username_snapshot"], str)
                else None
            ),
            email=(
                old_binding["email_snapshot"]
                if isinstance(old_binding["email_snapshot"], str)
                else None
            ),
        ),
    )


async def inspect_identity_migration(
    *,
    existing_user_id: str,
    old_issuer: str,
    old_subject: str,
    new_issuer: str,
    new_subject: str,
) -> IdentityMigrationReadback:
    """Dry-run an exact identity addition without acquiring mutation locks."""

    user_id, old_issuer, old_subject, new_issuer, new_subject = _validated_request(
        existing_user_id=existing_user_id,
        old_issuer=old_issuer,
        old_subject=old_subject,
        new_issuer=new_issuer,
        new_subject=new_subject,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        readback, _ = await _inspect_in_conn(
            conn,
            user_id=user_id,
            old_issuer=old_issuer,
            old_subject=old_subject,
            new_issuer=new_issuer,
            new_subject=new_subject,
            lock_user=False,
        )
    return readback


async def apply_identity_migration(
    *,
    existing_user_id: str,
    old_issuer: str,
    old_subject: str,
    new_issuer: str,
    new_subject: str,
    actor_id: str,
) -> IdentityMigrationReadback:
    """Idempotently add a verified broker identity to one exact AKB user."""

    user_id, old_issuer, old_subject, new_issuer, new_subject = _validated_request(
        existing_user_id=existing_user_id,
        old_issuer=old_issuer,
        old_subject=old_subject,
        new_issuer=new_issuer,
        new_subject=new_subject,
    )
    actor_id = _actor(actor_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Globally deterministic order prevents old/new inversions from
            # deadlocking and serializes with ordinary account projection.
            for key in sorted(
                {
                    _lock_key(old_issuer, old_subject),
                    _lock_key(new_issuer, new_subject),
                }
            ):
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    key,
                )

            before, old_binding = await _inspect_in_conn(
                conn,
                user_id=user_id,
                old_issuer=old_issuer,
                old_subject=old_subject,
                new_issuer=new_issuer,
                new_subject=new_subject,
                lock_user=True,
            )
            if before.state == "linked":
                return before

            identity_id = await conn.fetchval(
                """
                INSERT INTO external_identities (
                    user_id, issuer, subject, username_snapshot, email_snapshot
                )
                SELECT $1, $2, $3,
                       COALESCE($4, u.username),
                       COALESCE($5, u.email)
                  FROM users u
                 WHERE u.id = $1
                ON CONFLICT (issuer, subject) DO NOTHING
                RETURNING id
                """,
                user_id,
                new_issuer,
                new_subject,
                old_binding.username,
                old_binding.email,
            )
            if identity_id is None:
                # Defensive read-back for a writer that did not use the shared
                # advisory lock.  Never turn its binding into ours.
                owner = await conn.fetchval(
                    """
                    SELECT user_id FROM external_identities
                     WHERE issuer = $1 AND subject = $2
                    """,
                    new_issuer,
                    new_subject,
                )
                if owner != user_id:
                    raise IdentityMigrationError(
                        "identity_migration_new_binding_conflict"
                    )
            else:
                await emit_event(
                    conn,
                    "auth.external_identity_migrated",
                    actor_id=actor_id,
                    payload={
                        "user_id": str(user_id),
                        "old_issuer": old_issuer,
                        "old_subject": old_subject,
                        "new_issuer": new_issuer,
                        "new_subject": new_subject,
                    },
                )

    return IdentityMigrationReadback(
        user_id=user_id,
        state="linked",
        old_issuer=old_issuer,
        new_issuer=new_issuer,
        binding_changed=identity_id is not None,
    )


async def rollback_identity_migration(
    *,
    existing_user_id: str,
    old_issuer: str,
    old_subject: str,
    new_issuer: str,
    new_subject: str,
    actor_id: str,
) -> IdentityMigrationReadback:
    """Idempotently remove only the added broker binding.

    The old exact binding is revalidated under the same locks before deletion.
    Keycloak prelink cleanup is intentionally outside this database boundary
    and remains a one-time operator action after this read-back succeeds.
    """

    user_id, old_issuer, old_subject, new_issuer, new_subject = _validated_request(
        existing_user_id=existing_user_id,
        old_issuer=old_issuer,
        old_subject=old_subject,
        new_issuer=new_issuer,
        new_subject=new_subject,
    )
    actor_id = _actor(actor_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key in sorted(
                {
                    _lock_key(old_issuer, old_subject),
                    _lock_key(new_issuer, new_subject),
                }
            ):
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    key,
                )

            before, _ = await _inspect_in_conn(
                conn,
                user_id=user_id,
                old_issuer=old_issuer,
                old_subject=old_subject,
                new_issuer=new_issuer,
                new_subject=new_subject,
                lock_user=True,
            )
            if before.state == "ready_to_link":
                return before

            deleted_id = await conn.fetchval(
                """
                DELETE FROM external_identities
                 WHERE user_id = $1 AND issuer = $2 AND subject = $3
             RETURNING id
                """,
                user_id,
                new_issuer,
                new_subject,
            )
            if deleted_id is None:
                raise IdentityMigrationError(
                    "identity_migration_new_binding_conflict"
                )
            await emit_event(
                conn,
                "auth.external_identity_migration_rolled_back",
                actor_id=actor_id,
                payload={
                    "user_id": str(user_id),
                    "old_issuer": old_issuer,
                    "old_subject": old_subject,
                    "new_issuer": new_issuer,
                    "new_subject": new_subject,
                },
            )

    return IdentityMigrationReadback(
        user_id=user_id,
        state="ready_to_link",
        old_issuer=old_issuer,
        new_issuer=new_issuer,
        binding_changed=True,
    )
