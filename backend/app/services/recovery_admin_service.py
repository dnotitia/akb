"""Explicit, non-HTTP provisioning for the designated recovery administrator."""

from __future__ import annotations

from collections.abc import Callable
import re
import uuid
from typing import Literal

import asyncpg

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import (
    AKBError,
    RecoveryAdminConflictError,
    RecoveryAdminCredentialAuthorizationError,
    RecoveryAdminCredentialConflictError,
    RecoveryAdminCredentialUnavailableError,
    RecoveryAdminModeError,
    RecoveryAdminRetirementAuthorizationError,
    RecoveryAdminRetirementConflictError,
    ValidationError,
)
from app.repositories.events_repo import emit_event
from app.services.account_markers import RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL
from app.services.account_service import cleanup_token_roles
from app.services.auth_service import hash_password_async
from app.services.password_service import reset_password
from app.services.external_identity_contract import (
    OIDC_SUBJECT_MAX_LENGTH,
    bounded_nonempty_claim,
)
from app.services.role_sync import get_role_sync


# Fixed signed-bigint key: stable across processes and PostgreSQL upgrades,
# unlike a runtime hash whose implementation could change.
_PROVISIONING_LOCK_ID = 0x414B425245434F56  # "AKBRECOV"
# These fixed strings are intentionally invalid credential tombstones, not passwords.
_SSO_PASSWORD_SENTINEL = "!keycloak-sso:no-local-login!"  # nosec B105
# Every bcrypt hash starts with a versioned "$2x$<cost>$" prefix, so this
# distinguishes a real credential from any marker string.
_BCRYPT_HASH_PREFIX = re.compile(r"^\$2[aby]\$\d{2}\$")


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


def _is_local_recovery_credential(password_hash: str | None) -> bool:
    """Return whether a local recovery admin holds a credential we wrote.

    Only a bcrypt hash is legitimate. Anything else is an account state this
    service did not produce, so converging on it would adopt an identity of
    unknown provenance as the one account that can recover the installation —
    including either of the deliberate tombstones, which mark accounts that
    must never be re-adopted as the live recovery administrator.
    """
    if password_hash is None:
        return False
    return bool(_BCRYPT_HASH_PREFIX.match(password_hash))


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


async def _designated_user(conn, *, auth_provider: Literal["local", "keycloak"]):
    return await conn.fetchrow(
        """
        SELECT id, username, email, password_hash, is_admin, is_recovery_admin,
               auth_provider, account_status, account_kind
          FROM users
         WHERE is_recovery_admin AND auth_provider = $1
         FOR UPDATE
        """,
        auth_provider,
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
    credential_issued_by_akb: bool = True,
) -> dict:
    """Create the one local recovery admin, or converge on its exact identity.

    ``credential_issued_by_akb`` says whether this password is one AKB
    produced and handed back, which is the generated-output-file form of the
    provisioning command. That is the only shape in which provisioning
    delivers a credential to anybody, and delivery is what the forced
    credential change is about. Defaulting it to True keeps the requirement
    fail-closed for a caller that does not say.
    """
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
                row = await _designated_user(conn, auth_provider="local")
                if row is not None:
                    identity_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM external_identities WHERE user_id = $1",
                        row["id"],
                    )
                    # Convergence deliberately never touches the credential
                    # column: an issued password is not reset, and an unissued
                    # account is not silently given one.
                    if not (
                        row["username"] == username
                        and row["email"].lower() == email
                        and _is_local_recovery_credential(row["password_hash"])
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
                    # A credential AKB produced here is one AKB hands over —
                    # it leaves in the operator's output file — so it is
                    # delivered, and is owed a replacement exactly like the
                    # one `reset_password` issues. The SSO profile creates
                    # its product administrator the same way: a `temporary`
                    # credential plus the UPDATE_PASSWORD required action.
                    #
                    # A credential the caller supplied is a different event.
                    # The value existed before this process saw it, nobody
                    # received it from AKB, and whoever runs the installation
                    # already holds it — including when that is a machine,
                    # which then signs in as this account to establish the
                    # service principal the installation runs as. Arming the
                    # requirement there refuses the installer's own bootstrap
                    # for a delivery that never happened.
                    #
                    # Only creation arms it — the convergence branch above
                    # leaves the credential column alone, so re-running
                    # provisioning must not re-arm a marker the holder has
                    # already cleared.
                    row = await conn.fetchrow(
                        """
                        INSERT INTO users (
                            id, username, email, password_hash, is_admin,
                            is_recovery_admin, auth_provider, account_status, account_kind,
                            credential_change_required
                        ) VALUES ($1, $2, $3, $4, true, true, 'local', 'active', 'human',
                                  $5)
                        RETURNING id, username, email, password_hash, is_admin,
                                  is_recovery_admin, auth_provider, account_status, account_kind
                        """,
                        user_id,
                        username,
                        email,
                        password_hash,
                        credential_issued_by_akb,
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
    if bounded_nonempty_claim(subject, maximum=OIDC_SUBJECT_MAX_LENGTH) is None:
        raise ValidationError("subject exceeds maximum length")
    pool = await get_pool()
    created = False

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _lock_provisioning(conn)
                row = await _designated_user(conn, auth_provider="keycloak")
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


def _exact_expected(value: str, field: str) -> str:
    if not value or not value.strip():
        raise ValidationError(f"{field} is required")
    return value


def _retirement_proof(row) -> dict:
    return {
        "user_id": str(row["id"]),
        "username": row["username"],
        "email": row["email"],
        "account_status": row["account_status"],
        "is_admin": bool(row["is_admin"]),
        "is_recovery_admin": bool(row["is_recovery_admin"]),
        "account_kind": row["account_kind"],
        "auth_provider": row["auth_provider"],
    }


async def _require_independent_service_admin(
    conn,
    *,
    actor_user_id: str,
    actor_token_id: str,
    refusal: Callable[[], AKBError],
) -> uuid.UUID:
    """Resolve the caller as an independent service administrator, or refuse.

    Independent means it is not the designated recovery administrator and
    holds no external identity: the one account that can recover the
    installation must never be the authority over its own recovery.
    """
    try:
        user_id = uuid.UUID(actor_user_id)
        token_id = uuid.UUID(actor_token_id)
    except (AttributeError, TypeError, ValueError):
        raise refusal() from None

    row = await conn.fetchrow(
        """
        SELECT u.id, u.is_admin, u.is_recovery_admin, u.auth_provider,
               u.account_status, u.account_kind, t.key_class, t.scopes,
               NOT EXISTS (
                   SELECT 1 FROM external_identities e WHERE e.user_id = u.id
               ) AS has_no_external_identity
          FROM users u
          JOIN tokens t ON t.user_id = u.id
         WHERE u.id = $1 AND t.id = $2
           AND (t.expires_at IS NULL OR t.expires_at > NOW())
         FOR SHARE OF u, t
        """,
        user_id,
        token_id,
    )
    scopes = set(row["scopes"] or ("read", "write")) if row is not None else set()
    if (
        row is None
        or not row["is_admin"]
        or row["is_recovery_admin"]
        or row["auth_provider"] != "service"
        or row["account_status"] != "active"
        or row["account_kind"] != "service"
        or row["key_class"] not in {"pat", "service"}
        or not row["has_no_external_identity"]
        or not scopes.intersection({"write", "admin"})
    ):
        raise refusal()
    return user_id


async def _locked_recovery_rows(conn):
    return await conn.fetch(
        """
        SELECT u.id, u.username, u.email, u.password_hash, u.is_admin,
               u.is_recovery_admin, u.auth_provider, u.account_status,
               u.account_kind, u.tokens_revoked_before,
               (SELECT COUNT(*) FROM external_identities e WHERE e.user_id = u.id)
                   AS external_identity_count,
               EXISTS (
                   SELECT 1 FROM tokens t WHERE t.user_id = u.id
               ) AS has_tokens,
               EXISTS (
                   SELECT 1 FROM admin_browser_sessions s WHERE s.user_id = u.id
               ) AS has_admin_browser_sessions,
               EXISTS (
                   SELECT 1 FROM sso_browser_sessions s WHERE s.user_id = u.id
               ) AS has_sso_browser_sessions
          FROM users u
         WHERE u.is_recovery_admin
         ORDER BY u.id
         FOR UPDATE OF u
        """
    )


async def _locked_sso_recovery_identities(conn):
    rows = await conn.fetch(
        """
        SELECT e.user_id, e.issuer, e.subject
          FROM external_identities e
          JOIN users u ON u.id = e.user_id
         WHERE u.is_recovery_admin AND u.auth_provider = 'keycloak'
         ORDER BY e.user_id, e.issuer, e.subject
         FOR UPDATE OF e /* recovery-successor-authority-lock */
        """
    )
    identities: dict[uuid.UUID, list] = {}
    for row in rows:
        identities.setdefault(row["user_id"], []).append(row)
    return identities


async def _locked_retirement_collisions(
    conn,
    *,
    expected_username: str,
    expected_email: str,
):
    return await conn.fetch(
        """
        SELECT u.id, u.username, u.email, u.password_hash, u.is_admin,
               u.is_recovery_admin, u.auth_provider, u.account_status,
               u.account_kind, u.tokens_revoked_before,
               (SELECT COUNT(*) FROM external_identities e WHERE e.user_id = u.id)
                   AS external_identity_count,
               EXISTS (
                   SELECT 1 FROM tokens t WHERE t.user_id = u.id
               ) AS has_tokens,
               EXISTS (
                   SELECT 1 FROM admin_browser_sessions s WHERE s.user_id = u.id
               ) AS has_admin_browser_sessions,
               EXISTS (
                   SELECT 1 FROM sso_browser_sessions s WHERE s.user_id = u.id
               ) AS has_sso_browser_sessions
          FROM users u
         WHERE u.username = $1 OR u.email = $2
         ORDER BY u.id
         FOR UPDATE OF u
        """,
        expected_username,
        expected_email,
    )


def _is_exact_active_local_recovery(
    row,
    *,
    expected_username: str,
    expected_email: str,
) -> bool:
    return bool(
        row["username"] == expected_username
        and row["email"] == expected_email
        and row["is_admin"]
        and row["is_recovery_admin"]
        and row["auth_provider"] == "local"
        and row["account_status"] == "active"
        and row["account_kind"] == "human"
        and row["external_identity_count"] == 0
    )


def _is_exact_active_sso_recovery(row, identities) -> bool:
    return bool(
        row["password_hash"] == _SSO_PASSWORD_SENTINEL
        and row["is_admin"]
        and row["is_recovery_admin"]
        and row["auth_provider"] == "keycloak"
        and row["account_status"] == "active"
        and row["account_kind"] == "human"
        and row["external_identity_count"] == 1
        and len(identities) == 1
        and identities[0]["issuer"] == settings.keycloak_issuer
        and bounded_nonempty_claim(
            identities[0]["subject"], maximum=OIDC_SUBJECT_MAX_LENGTH
        )
        is not None
    )


def _is_exact_retired_tombstone(
    row,
    *,
    expected_username: str,
    expected_email: str,
) -> bool:
    return bool(
        row["username"] == expected_username
        and row["email"] == expected_email
        and row["password_hash"] == RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL
        and not row["is_admin"]
        and not row["is_recovery_admin"]
        and row["auth_provider"] == "local"
        and row["account_status"] == "suspended"
        and row["account_kind"] == "human"
        and row["external_identity_count"] == 0
        and not row["has_tokens"]
        and not row["has_admin_browser_sessions"]
        and not row["has_sso_browser_sessions"]
    )


async def _pending_token_cleanup(conn, user_id: uuid.UUID) -> list[uuid.UUID]:
    pending = await conn.fetch(
        """
        SELECT token_id
          FROM account_token_cleanup
         WHERE user_id = $1 AND completed_at IS NULL
         ORDER BY requested_at, token_id
        """,
        user_id,
    )
    return [record["token_id"] for record in pending]


async def retire_local_recovery_admin(
    *,
    expected_username: str,
    expected_email: str,
    actor_user_id: str,
    actor_token_id: str,
) -> dict:
    """Retire the exact local recovery administrator after an SSO cutover.

    Credential rows and account denial commit together. Derived PostgreSQL
    token roles are then removed strictly through the durable cleanup ledger;
    an incomplete cleanup returns 503 and an exact retry resumes it without
    restoring credentials or emitting a duplicate retirement event.
    """
    _require_mode("sso")
    expected_username = _exact_expected(expected_username, "expected_username")
    expected_email = _exact_expected(expected_email, "expected_email")
    pool = await get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            actor_id = await _require_independent_service_admin(
                conn,
                actor_user_id=actor_user_id,
                actor_token_id=actor_token_id,
                refusal=RecoveryAdminRetirementAuthorizationError,
            )
            await _lock_provisioning(conn)
            sso_identities = await _locked_sso_recovery_identities(conn)
            designated = await _locked_recovery_rows(conn)
            local_designated = [
                row for row in designated if row["auth_provider"] == "local"
            ]
            sso_successors = []
            for row in designated:
                if row["auth_provider"] != "keycloak":
                    continue
                identities = sso_identities.get(row["id"], [])
                if _is_exact_active_sso_recovery(row, identities):
                    sso_successors.append(row)
            if (
                len(local_designated) > 1
                or len(sso_successors) != 1
                or len(designated) != len(local_designated) + len(sso_successors)
            ):
                raise RecoveryAdminRetirementConflictError()

            if local_designated:
                current = local_designated[0]
                if current["id"] == actor_id or not _is_exact_active_local_recovery(
                    current,
                    expected_username=expected_username,
                    expected_email=expected_email,
                ):
                    raise RecoveryAdminRetirementConflictError()

                deleted = await conn.fetch(
                    "DELETE FROM tokens WHERE user_id = $1 RETURNING id",
                    current["id"],
                )
                token_ids = [record["id"] for record in deleted]
                if token_ids:
                    await conn.executemany(
                        """
                        INSERT INTO account_token_cleanup (token_id, user_id)
                        VALUES ($1, $2)
                        ON CONFLICT (token_id) DO NOTHING
                        """,
                        [(token_id, current["id"]) for token_id in token_ids],
                    )
                await conn.execute(
                    "DELETE FROM admin_browser_sessions WHERE user_id = $1",
                    current["id"],
                )
                await conn.execute(
                    "DELETE FROM sso_browser_sessions WHERE user_id = $1",
                    current["id"],
                )
                retired = await conn.fetchrow(
                    """
                    UPDATE users
                       SET is_recovery_admin = false,
                           is_admin = false,
                           account_status = 'suspended',
                           password_hash = $2,
                           -- The tombstone is not a credential anyone was
                           -- handed, so there is nothing left to replace.
                           credential_change_required = false,
                           tokens_revoked_before = NOW(),
                           updated_at = NOW()
                     WHERE id = $1
                 RETURNING id, username, email, is_admin, is_recovery_admin,
                           auth_provider, account_status, account_kind
                    """,
                    current["id"],
                    RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL,
                )
                await emit_event(
                    conn,
                    "auth.recovery_admin_retired",
                    actor_id=str(actor_id),
                    payload={
                        "user_id": str(current["id"]),
                        "account_status": "suspended",
                        "is_admin": False,
                        "is_recovery_admin": False,
                        "revoked_token_ids": [str(token_id) for token_id in token_ids],
                    },
                )
            else:
                collisions = await _locked_retirement_collisions(
                    conn,
                    expected_username=expected_username,
                    expected_email=expected_email,
                )
                if (
                    len(collisions) != 1
                    or collisions[0]["id"] == actor_id
                    or not _is_exact_retired_tombstone(
                        collisions[0],
                        expected_username=expected_username,
                        expected_email=expected_email,
                    )
                ):
                    raise RecoveryAdminRetirementConflictError()
                retired = collisions[0]

            pending_token_ids = await _pending_token_cleanup(conn, retired["id"])

    await cleanup_token_roles(pool, retired["id"], pending_token_ids)
    return _retirement_proof(retired)


CredentialIssueMethod = Literal["recovery_admin_api", "recovery_admin_cli"]


def _is_issuable_local_recovery(row, *, expected_username: str, expected_email: str) -> bool:
    """Return whether this row is the exact live account we may rotate.

    A retired administrator fails every clause: retirement clears both admin
    flags, suspends the account, and leaves its own tombstone marker, so it
    can never be re-entered through this path.
    """
    return bool(
        row is not None
        and row["username"] == expected_username
        and (row["email"] or "").lower() == expected_email
        and row["is_admin"]
        and row["is_recovery_admin"]
        and row["auth_provider"] == "local"
        and row["account_status"] == "active"
        and row["account_kind"] == "human"
        and _is_local_recovery_credential(row["password_hash"])
    )


async def issue_recovery_admin_credential(
    *,
    expected_username: str,
    expected_email: str,
    method: CredentialIssueMethod,
    actor_user_id: str | None = None,
    actor_token_id: str | None = None,
) -> dict:
    """Replace the credential of the exact designated recovery administrator.

    Installation leaves the account holding a credential, so this is rotation:
    the response to one that leaked, was lost, or must be taken back. The
    previous credential stops working, including one currently in use — that
    is what a compromise response requires. The returned credential is the
    only copy: it is bcrypt-hashed into the account and never stored, logged,
    or placed in an audit payload. Audit records that a rotation happened, by
    whom, and for which account.

    ``method`` selects the authority. ``recovery_admin_api`` requires an
    independent service administrator token and is re-validated here rather
    than trusted from the route. ``recovery_admin_cli`` is the break-glass
    path, whose authority is workspace shell access and which therefore has
    no authenticated principal at all — supplying one is refused so an API
    caller cannot borrow the unauthenticated route.
    """
    expected_username = _exact_expected(expected_username, "expected_username")
    expected_email = _exact_expected(expected_email, "expected_email").lower()
    if method == "recovery_admin_cli":
        if actor_user_id is not None or actor_token_id is not None:
            raise RecoveryAdminCredentialAuthorizationError()
    elif actor_user_id is None or actor_token_id is None:
        raise RecoveryAdminCredentialAuthorizationError()

    mode = settings.require_auth_mode()
    pool = await get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            actor_id: uuid.UUID | None = None
            if actor_user_id is not None and actor_token_id is not None:
                actor_id = await _require_independent_service_admin(
                    conn,
                    actor_user_id=actor_user_id,
                    actor_token_id=actor_token_id,
                    refusal=RecoveryAdminCredentialAuthorizationError,
                )
            await _lock_provisioning(conn)
            row = await _designated_user(
                conn,
                auth_provider="local" if mode == "local" else "keycloak",
            )
            if mode != "local":
                # The account is reachable — installation wrote a credential
                # into the identity provider and handed it over — but nothing
                # in a running AKB can replace that credential. The permanent
                # management client is deliberately without `manage-users`,
                # and the temporary bootstrap client that created the product
                # administrator is deleted and proven dead before the install
                # writes its retirement receipt. Rotating here needs an
                # authority an operator creates and destroys, which does not
                # exist yet.
                raise RecoveryAdminCredentialUnavailableError()
            if not _is_issuable_local_recovery(
                row,
                expected_username=expected_username,
                expected_email=expected_email,
            ):
                raise RecoveryAdminCredentialConflictError()
            user_id = row["id"]
            username = row["username"]
            email = row["email"]

    # The delegate opens its own transaction and takes its own row lock, so
    # the verification above must have committed first. The window is safe:
    # `users.username` is unique and retirement does not release it, and the
    # delegate refuses a suspended account — which is exactly what a
    # retirement landing in the window would have produced.
    credential, issued_username = await reset_password(
        username=username,
        actor_id=str(actor_id) if actor_id is not None else None,
        method=method,
    )

    async with pool.acquire() as conn:
        await emit_event(
            conn,
            "auth.recovery_admin_credential_issued",
            actor_id=str(actor_id) if actor_id is not None else None,
            payload={
                "user_id": str(user_id),
                "username": issued_username,
                "auth_mode": mode,
                "method": method,
            },
        )
    return {
        "user_id": str(user_id),
        "username": issued_username,
        "email": email,
        "auth_mode": mode,
        "credential": credential,
    }
