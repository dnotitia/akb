"""Administrative account projection for external control planes."""

from __future__ import annotations

import uuid

import asyncpg

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import (
    AccountSuspendedError,
    CredentialCleanupIncompleteError,
    ExternalAuthDisabledError,
    ExternalIdentityConflictError,
    ExternalIdentityIssuerMismatchError,
    NotFoundError,
    RecoveryAdminProtectedError,
    ServiceIdentityAdoptionError,
    ValidationError,
)
from app.repositories.events_repo import emit_event
from app.services.account_markers import (
    RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL,
    is_retired_recovery_admin_password,
)
from app.services.auth_service import (
    REVOKE_REASON_ADMIN,
    _hash_token,
    _revoke_sessions_in_conn,
    _unique_username,
)
from app.services.role_sync import get_role_sync


def _presented_issuer(issuer: str) -> str:
    """Refuse a binding under an issuer this runtime will never present.

    ``get_managed_account_state`` already compares an issuer it is *given*
    against ``settings.keycloak_issuer`` before it will read anything. The
    write path had no such comparison, and that asymmetry is the whole defect:
    a control plane could create an exact binding under its own issuer, be told
    it succeeded, and leave the person it named unable to sign in — because
    ``invite_only`` matches the exact pair and theirs is stored under an issuer
    that never appears on a token here.

    In local mode the answer is not "some other issuer" but "none". Writing an
    external binding there is worse than inert: the same call sets
    ``auth_provider = 'keycloak'``, and local sign-in requires ``'local'``, so
    it locks the account out of the only credential it has.
    """

    if settings.require_auth_mode() != "sso" or not settings.keycloak_enabled:
        raise ExternalAuthDisabledError()
    expected = settings.keycloak_issuer
    if issuer != expected:
        raise ExternalIdentityIssuerMismatchError(expected)
    return issuer


_HUMAN_SENTINEL_HASH = "!keycloak-sso:no-local-login!"
_SERVICE_SENTINEL_HASH = "!service-account:no-local-login!"


def _required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field} is required")
    return normalized


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except AttributeError, TypeError, ValueError:
        raise ValidationError(f"{field} must be a UUID") from None


def _user_result(row) -> dict:
    return {
        "user_id": str(row["id"]),
        "username": row["username"],
        "email": row["email"],
        "display_name": row["display_name"],
        "is_admin": row["is_admin"],
        "account_status": row["account_status"],
        "account_kind": row["account_kind"],
        "auth_provider": row["auth_provider"],
    }


async def _fetch_user(conn, user_id: uuid.UUID):
    return await conn.fetchrow(
        """
        SELECT id, username, email, display_name, is_admin, is_recovery_admin,
               auth_provider, account_status, account_kind
          FROM users WHERE id = $1
        """,
        user_id,
    )


async def ensure_human_external_identity(
    *,
    issuer: str,
    subject: str,
    email: str,
    display_name: str | None,
    actor_id: str,
    existing_user_id: str | None = None,
    prepare_suspended: bool = False,
) -> dict:
    issuer = _presented_issuer(_required(issuer, "issuer"))
    subject = _required(subject, "subject")
    email = _required(email, "email").lower()
    existing_user_uuid = _uuid(existing_user_id, "existing_user_id") if existing_user_id is not None else None
    pool = await get_pool()
    new_user_id: uuid.UUID | None = None
    token_ids: list[uuid.UUID] = []

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"external-identity:{len(issuer)}:{issuer}{subject}",
                )
                bound = await conn.fetchrow(
                    """
                    SELECT u.id, u.account_kind, u.password_hash
                      FROM external_identities e
                      JOIN users u ON u.id = e.user_id
                     WHERE e.issuer = $1 AND e.subject = $2
                     FOR UPDATE OF u
                    """,
                    issuer,
                    subject,
                )
                if bound is not None:
                    if is_retired_recovery_admin_password(bound["password_hash"]):
                        raise RecoveryAdminProtectedError()
                    if bound["account_kind"] != "human" or (
                        existing_user_uuid is not None and bound["id"] != existing_user_uuid
                    ):
                        raise ExternalIdentityConflictError()
                    user_id = bound["id"]
                    await conn.execute(
                        """
                        UPDATE users
                           SET email = $2,
                               display_name = COALESCE($3, display_name),
                               auth_provider = 'keycloak', updated_at = NOW()
                         WHERE id = $1
                        """,
                        user_id,
                        email,
                        display_name,
                    )
                    await conn.execute(
                        """
                        UPDATE external_identities
                           SET email_snapshot = $3, last_seen_at = NOW()
                         WHERE user_id = $1 AND issuer = $2 AND subject = $4
                        """,
                        user_id,
                        issuer,
                        email,
                        subject,
                    )
                else:
                    if existing_user_uuid is not None:
                        target = await conn.fetchrow(
                            """
                            SELECT id, account_kind, password_hash
                              FROM users WHERE id = $1
                             FOR UPDATE
                            """,
                            existing_user_uuid,
                        )
                        if target is None:
                            raise NotFoundError("User", str(existing_user_uuid))
                        if is_retired_recovery_admin_password(target["password_hash"]):
                            raise RecoveryAdminProtectedError()
                        if target["account_kind"] != "human":
                            raise ExternalIdentityConflictError()
                        email_owner = await conn.fetchval(
                            "SELECT id FROM users WHERE email = $1",
                            email,
                        )
                        if email_owner is not None and email_owner != existing_user_uuid:
                            raise ExternalIdentityConflictError()
                        user_id = existing_user_uuid
                        await conn.execute(
                            """
                            UPDATE users
                               SET email = $2, auth_provider = 'keycloak',
                                   display_name = COALESCE($3, display_name),
                                   updated_at = NOW()
                             WHERE id = $1
                            """,
                            user_id,
                            email,
                            display_name,
                        )
                    else:
                        rows = await conn.fetch(
                            """
                            SELECT id, account_kind, password_hash
                              FROM users WHERE email = $1
                             FOR UPDATE
                            """,
                            email,
                        )
                        if len(rows) > 1:
                            raise ExternalIdentityConflictError()
                        if rows:
                            user_id = rows[0]["id"]
                            if is_retired_recovery_admin_password(rows[0]["password_hash"]):
                                raise RecoveryAdminProtectedError()
                            if rows[0]["account_kind"] != "human":
                                raise ExternalIdentityConflictError()
                            has_identity = await conn.fetchval(
                                "SELECT EXISTS (SELECT 1 FROM external_identities WHERE user_id = $1)",
                                user_id,
                            )
                            if has_identity:
                                raise ExternalIdentityConflictError()
                            await conn.execute(
                                """
                                UPDATE users
                                   SET auth_provider = 'keycloak',
                                       display_name = COALESCE($2, display_name),
                                       updated_at = NOW()
                                 WHERE id = $1
                                """,
                                user_id,
                                display_name,
                            )
                        else:
                            user_id = uuid.uuid4()
                            username = await _unique_username(conn, email.split("@")[0])
                            await conn.execute(
                                """
                                INSERT INTO users (
                                    id, username, email, password_hash, display_name,
                                    is_admin, auth_provider, account_status, account_kind
                                ) VALUES ($1, $2, $3, $4, $5, false, 'keycloak',
                                          'active', 'human')
                                """,
                                user_id,
                                username,
                                email,
                                _HUMAN_SENTINEL_HASH,
                                display_name,
                            )
                            new_user_id = user_id

                    await conn.execute(
                        """
                        INSERT INTO external_identities (
                            user_id, issuer, subject, email_snapshot
                        ) VALUES ($1, $2, $3, $4)
                        """,
                        user_id,
                        issuer,
                        subject,
                        email,
                    )

                await emit_event(
                    conn,
                    "auth.external_identity_ensured",
                    actor_id=actor_id,
                    payload={
                        "user_id": str(user_id),
                        "issuer": issuer,
                        "subject": subject,
                    },
                )
                if prepare_suspended:
                    token_ids = await _suspend_user_in_conn(
                        conn,
                        user_id,
                        actor_id=actor_id,
                    )
        except asyncpg.UniqueViolationError:
            raise ExternalIdentityConflictError() from None

        row = await _fetch_user(conn, user_id)

    if new_user_id is not None:
        await get_role_sync().on_user_create(new_user_id)
    if prepare_suspended:
        await cleanup_token_roles(pool, user_id, token_ids)
    return _user_result(row)


async def ensure_service_user(
    *,
    username: str,
    email: str,
    display_name: str | None,
    actor_id: str,
    is_admin: bool = False,
) -> dict:
    username = _required(username, "username")
    email = _required(email, "email").lower()
    pool = await get_pool()
    new_user_id: uuid.UUID | None = None

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"service-user:{len(username)}:{username}{email}",
                )
                rows = await conn.fetch(
                    """
                    SELECT u.id, u.username, u.email, u.account_kind,
                           u.auth_provider, u.account_status,
                           EXISTS (
                               SELECT 1 FROM external_identities e
                                WHERE e.user_id = u.id
                           ) AS has_external_identity
                      FROM users u
                     WHERE u.username = $1 OR u.email = $2
                     FOR UPDATE OF u
                    """,
                    username,
                    email,
                )
                if rows:
                    if (
                        len(rows) != 1
                        or rows[0]["username"] != username
                        or rows[0]["email"] != email
                        or rows[0]["account_kind"] != "service"
                        or rows[0]["auth_provider"] != "service"
                        or rows[0]["has_external_identity"]
                    ):
                        raise ExternalIdentityConflictError()
                    if rows[0]["account_status"] != "active":
                        raise AccountSuspendedError()
                    user_id = rows[0]["id"]
                    await conn.execute(
                        """
                        UPDATE users
                           SET display_name = COALESCE($2, display_name),
                               is_admin = $3,
                               updated_at = NOW()
                         WHERE id = $1
                        """,
                        user_id,
                        display_name,
                        is_admin,
                    )
                else:
                    user_id = uuid.uuid4()
                    await conn.execute(
                        """
                        INSERT INTO users (
                            id, username, email, password_hash, display_name,
                            is_admin, auth_provider, account_status, account_kind
                        ) VALUES ($1, $2, $3, $4, $5, $6, 'service',
                                  'active', 'service')
                        """,
                        user_id,
                        username,
                        email,
                        _SERVICE_SENTINEL_HASH,
                        display_name,
                        is_admin,
                    )
                    new_user_id = user_id

                await emit_event(
                    conn,
                    "auth.service_user_ensured",
                    actor_id=actor_id,
                    payload={"user_id": str(user_id), "is_admin": is_admin},
                )
        except asyncpg.UniqueViolationError:
            raise ExternalIdentityConflictError() from None

        row = await _fetch_user(conn, user_id)

    if new_user_id is not None:
        await get_role_sync().on_user_create(new_user_id)
    return _user_result(row)


async def adopt_current_admin_as_service(
    *,
    user_id: str,
    token_id: str,
    expected_username: str,
    expected_email: str,
    actor_id: str,
) -> dict:
    """Atomically adopt the current local bootstrap admin and its PAT.

    The raw credential never changes, so a controller crash cannot strand the
    workspace between AKB state and Secret delivery. Every other token is
    denied in the transaction and its derived PG role is then cleaned strictly.
    """

    user_uuid = _uuid(user_id, "user_id")
    token_uuid = _uuid(token_id, "token_id")
    expected_username = _required(expected_username, "expected_username")
    expected_email = _required(expected_email, "expected_email").lower()
    pool = await get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                """
                SELECT id, username, email, password_hash, display_name, is_admin,
                       is_recovery_admin, auth_provider, account_status, account_kind,
                       EXISTS (
                           SELECT 1 FROM external_identities e
                            WHERE e.user_id = users.id
                       ) AS has_external_identity
                  FROM users
                 WHERE id = $1
                   FOR UPDATE
                """,
                user_uuid,
            )
            if (
                current is None
                or current["username"] != expected_username
                or current["email"].lower() != expected_email
                or current["account_status"] != "active"
                or not current["is_admin"]
                or current["has_external_identity"]
                or current["account_kind"] not in {"human", "service"}
                or (current["account_kind"] == "human" and current["auth_provider"] != "local")
                or (current["account_kind"] == "service" and current["auth_provider"] != "service")
            ):
                raise ServiceIdentityAdoptionError()

            token = await conn.fetchrow(
                """
                SELECT user_id, key_class, scopes, vault_scope, expires_at
                  FROM tokens
                 WHERE id = $1
                   FOR UPDATE
                """,
                token_uuid,
            )
            if (
                token is None
                or token["user_id"] != user_uuid
                or token["key_class"] not in {"pat", "service"}
                or token["vault_scope"] is not None
                or token["expires_at"] is not None
            ):
                raise ServiceIdentityAdoptionError()

            user_changed = (
                current["account_kind"] != "service"
                or current["auth_provider"] != "service"
                or current["password_hash"] != _SERVICE_SENTINEL_HASH
                or current["is_recovery_admin"]
            )
            wanted_scopes = ["read", "write", "admin"]
            token_changed = token["key_class"] != "service" or list(token["scopes"] or []) != wanted_scopes
            if user_changed:
                await conn.execute(
                    """
                    UPDATE users
                       SET account_kind = 'service',
                           auth_provider = 'service',
                           password_hash = $2,
                           is_recovery_admin = false,
                           tokens_revoked_before = CASE
                               WHEN account_kind = 'human' THEN NOW()
                               ELSE tokens_revoked_before
                           END,
                           updated_at = NOW()
                     WHERE id = $1
                    """,
                    user_uuid,
                    _SERVICE_SENTINEL_HASH,
                )
            if token_changed:
                await conn.execute(
                    """
                    UPDATE tokens
                       SET key_class = 'service', scopes = $3::text[]
                     WHERE id = $1 AND user_id = $2
                    """,
                    token_uuid,
                    user_uuid,
                    wanted_scopes,
                )
            deleted = await conn.fetch(
                """
                DELETE FROM tokens
                 WHERE user_id = $1 AND id <> $2
             RETURNING id
                """,
                user_uuid,
                token_uuid,
            )
            if deleted:
                await conn.executemany(
                    """
                    INSERT INTO account_token_cleanup (token_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT (token_id) DO NOTHING
                    """,
                    [(record["id"], user_uuid) for record in deleted],
                )
            pending = await conn.fetch(
                """
                SELECT token_id
                  FROM account_token_cleanup
                 WHERE user_id = $1 AND completed_at IS NULL
                 ORDER BY requested_at, token_id
                """,
                user_uuid,
            )
            pending_token_ids = [record["token_id"] for record in pending]
            if user_changed or token_changed or deleted:
                await emit_event(
                    conn,
                    "auth.bootstrap_service_adopted",
                    actor_id=actor_id,
                    payload={
                        "user_id": user_id,
                        "token_id": token_id,
                        "revoked_token_ids": [str(value) for value in pending_token_ids],
                    },
                )
            adopted = await _fetch_user(conn, user_uuid)
            if adopted is None or adopted["is_recovery_admin"] is not False:
                raise ServiceIdentityAdoptionError()

    await cleanup_token_roles(pool, user_uuid, pending_token_ids)
    return {
        **_user_result(adopted),
        "is_recovery_admin": adopted["is_recovery_admin"],
        "token_id": token_id,
        "key_class": "service",
        "revoked_token_ids": [str(value) for value in pending_token_ids],
    }


async def get_user(user_id: str) -> dict:
    user_uuid = _uuid(user_id, "user_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _fetch_user(conn, user_uuid)
    if row is None:
        raise NotFoundError("User", user_id)
    return _user_result(row)


async def get_human_user_by_email(email: str) -> dict:
    normalized_email = _required(email, "email").lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.username, u.email, u.display_name, u.is_admin,
                   u.auth_provider, u.account_status, u.account_kind,
                   EXISTS (
                       SELECT 1 FROM external_identities e WHERE e.user_id = u.id
                   ) AS has_external_identity
              FROM users u
             WHERE u.email = $1 AND u.account_kind = 'human'
            """,
            normalized_email,
        )
    if row is None:
        raise NotFoundError("Human user", normalized_email)
    return {**_user_result(row), "has_external_identity": row["has_external_identity"]}


async def get_user_by_external_identity(issuer: str, subject: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.username, u.email, u.display_name, u.is_admin,
                   u.auth_provider, u.account_status, u.account_kind
              FROM external_identities e
              JOIN users u ON u.id = e.user_id
             WHERE e.issuer = $1 AND e.subject = $2
            """,
            _required(issuer, "issuer"),
            _required(subject, "subject"),
        )
    if row is None:
        raise NotFoundError("External identity", f"{issuer}#{subject}")
    return _user_result(row)


def _expected_managed_humans(
    expected_humans: list[dict[str, str]],
) -> dict[uuid.UUID, str]:
    if not isinstance(expected_humans, list) or not expected_humans:
        raise ValidationError("expected_humans must contain at least one account")
    if len(expected_humans) > 10_000:
        raise ValidationError("expected_humans exceeds the managed account limit")

    expected: dict[uuid.UUID, str] = {}
    subjects: set[str] = set()
    for item in expected_humans:
        if not isinstance(item, dict):
            raise ValidationError("expected_humans entries must be objects")
        raw_user_id = _required(item.get("user_id", ""), "user_id")
        user_id = _uuid(raw_user_id, "user_id")
        if str(user_id) != raw_user_id:
            raise ValidationError("user_id must be a canonical UUID")
        subject = _required(item.get("subject", ""), "subject")
        if user_id in expected:
            raise ValidationError("expected_humans contains a duplicate user_id")
        if subject in subjects:
            raise ValidationError("expected_humans contains a duplicate subject")
        expected[user_id] = subject
        subjects.add(subject)
    return expected


async def get_managed_account_state(
    *,
    issuer: str,
    expected_humans: list[dict[str, str]],
) -> dict:
    """Compare the running AKB profile and active humans with platform intent.

    The response deliberately contains only counts and stable issue codes. It
    never returns email, subject, password, token, or credential material.
    """

    issuer = _required(issuer, "issuer")
    if issuer != settings.keycloak_issuer:
        raise ValidationError("issuer does not match the running AKB configuration")
    expected = _expected_managed_humans(expected_humans)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.id, u.auth_provider,
                   COALESCE(
                     array_agg(e.subject ORDER BY e.subject)
                       FILTER (WHERE e.user_id IS NOT NULL),
                     ARRAY[]::text[]
                   ) AS subjects
              FROM users u
              LEFT JOIN external_identities e
                ON e.user_id = u.id AND e.issuer = $1
             WHERE u.account_kind = 'human'
               AND u.account_status = 'active'
             GROUP BY u.id, u.auth_provider
             ORDER BY u.id
            """,
            issuer,
        )

    observed: dict[uuid.UUID, tuple[str, set[str]]] = {}
    for row in rows:
        user_id = uuid.UUID(str(row["id"]))
        if user_id in observed:
            raise RuntimeError("managed account query returned a duplicate user")
        observed[user_id] = (
            str(row["auth_provider"]),
            {str(subject) for subject in row["subjects"]},
        )

    issues: set[str] = set()
    if set(observed) != set(expected):
        issues.add("active_human_set_mismatch")
    if any(provider != "keycloak" for provider, _ in observed.values()):
        issues.add("human_auth_provider_mismatch")
    if any(observed.get(user_id, ("", set()))[1] != {subject} for user_id, subject in expected.items()):
        issues.add("expected_identity_mismatch")

    account_issues = set(issues)
    profile_ready = (
        settings.sso_human_auth_enabled
        and settings.keycloak_enabled
        and settings.keycloak_enrollment_mode == "invite_only"
        and not settings.keycloak_link_by_email
        and settings.keycloak_require_verified_email
    )
    if not profile_ready:
        issues.add("managed_auth_profile_mismatch")

    return {
        "ready": not issues,
        "account_inventory_ready": not account_issues,
        "managed_auth_profile_ready": profile_ready,
        "expected_active_humans": len(expected),
        "observed_active_humans": len(observed),
        "issues": sorted(issues),
    }


async def set_user_admin(user_id: str, *, is_admin: bool, actor_id: str) -> dict:
    user_uuid = _uuid(user_id, "user_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                """
                SELECT account_status, account_kind, is_recovery_admin
                  FROM users WHERE id = $1
                   FOR UPDATE
                """,
                user_uuid,
            )
            if current is None or current["account_kind"] != "human":
                raise NotFoundError("Human user", user_id)
            if not is_admin and current["is_recovery_admin"]:
                raise RecoveryAdminProtectedError()
            if is_admin and current["account_status"] != "active":
                raise AccountSuspendedError()
            row = await conn.fetchrow(
                """
                UPDATE users
                   SET is_admin = $2, updated_at = NOW()
                 WHERE id = $1
             RETURNING id, username, email, display_name, is_admin,
                       auth_provider, account_status, account_kind
                """,
                user_uuid,
                is_admin,
            )
            if not is_admin:
                # A demoted account must not regain an old admin browser
                # handle if it is promoted again before that handle is used.
                await conn.execute(
                    "DELETE FROM admin_browser_sessions WHERE user_id = $1",
                    user_uuid,
                )
            await emit_event(
                conn,
                "auth.user_role_changed",
                actor_id=actor_id,
                payload={"user_id": user_id, "is_admin": is_admin},
            )
    return _user_result(row)


async def cleanup_token_roles(pool, user_id: uuid.UUID, token_ids: list[uuid.UUID]) -> None:
    """Strictly finish durable token-role cleanup before reporting success."""
    failed: list[str] = []
    for token_id in token_ids:
        try:
            await get_role_sync().revoke_token_role_strict(token_id)
        except Exception as error:  # noqa: BLE001
            failed.append(str(token_id))
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE account_token_cleanup
                       SET attempts = attempts + 1, last_error = $2
                     WHERE token_id = $1 AND user_id = $3
                    """,
                    token_id,
                    type(error).__name__,
                    user_id,
                )
        else:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE account_token_cleanup
                       SET attempts = attempts + 1, completed_at = NOW(),
                           last_error = NULL
                     WHERE token_id = $1 AND user_id = $2
                    """,
                    token_id,
                    user_id,
                )
    if failed:
        raise CredentialCleanupIncompleteError(failed)


async def _suspend_user_in_conn(
    conn,
    user_id: uuid.UUID,
    *,
    actor_id: str,
) -> list[uuid.UUID]:
    row = await conn.fetchrow(
        "SELECT account_status FROM users WHERE id = $1 FOR UPDATE",
        user_id,
    )
    if row is None:
        raise NotFoundError("User", str(user_id))

    if row["account_status"] == "active":
        await conn.execute(
            """
            UPDATE users
               SET account_status = 'suspended', updated_at = NOW()
             WHERE id = $1
            """,
            user_id,
        )
        await _revoke_sessions_in_conn(
            conn,
            user_id,
            actor_id=actor_id,
            reason=REVOKE_REASON_ADMIN,
        )
        # Browser handles carry no self-contained status cutoff. Delete them
        # in the same account-state transaction so suspend -> activate cannot
        # revive a cookie that was never observed while suspended.
        await conn.execute(
            "DELETE FROM admin_browser_sessions WHERE user_id = $1",
            user_id,
        )
        await conn.execute(
            "DELETE FROM sso_browser_sessions WHERE user_id = $1",
            user_id,
        )
        deleted = await conn.fetch(
            "DELETE FROM tokens WHERE user_id = $1 RETURNING id",
            user_id,
        )
        token_ids = [record["id"] for record in deleted]
        if token_ids:
            await conn.executemany(
                """
                INSERT INTO account_token_cleanup (token_id, user_id)
                VALUES ($1, $2)
                ON CONFLICT (token_id) DO NOTHING
                """,
                [(token_id, user_id) for token_id in token_ids],
            )
        await emit_event(
            conn,
            "auth.account_suspended",
            actor_id=actor_id,
            payload={
                "user_id": str(user_id),
                "revoked_token_ids": [str(token_id) for token_id in token_ids],
            },
        )
        return token_ids

    pending = await conn.fetch(
        """
        SELECT token_id FROM account_token_cleanup
         WHERE user_id = $1 AND completed_at IS NULL
         ORDER BY requested_at, token_id
        """,
        user_id,
    )
    return [record["token_id"] for record in pending]


async def suspend_user(user_id: str, *, actor_id: str) -> dict:
    user_uuid = _uuid(user_id, "user_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            token_ids = await _suspend_user_in_conn(
                conn,
                user_uuid,
                actor_id=actor_id,
            )

    await cleanup_token_roles(pool, user_uuid, token_ids)
    return {
        "user_id": user_id,
        "account_status": "suspended",
        "revoked_token_ids": [str(token_id) for token_id in token_ids],
    }


async def activate_user(user_id: str, *, actor_id: str) -> dict:
    user_uuid = _uuid(user_id, "user_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users
                   SET account_status = 'active', updated_at = NOW()
                 WHERE id = $1 AND password_hash <> $2
             RETURNING id, username, email, display_name, is_admin,
                       auth_provider, account_status, account_kind
                """,
                user_uuid,
                RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL,
            )
            if row is None:
                current = await conn.fetchrow(
                    "SELECT password_hash FROM users WHERE id = $1 FOR UPDATE",
                    user_uuid,
                )
                if current is None:
                    raise NotFoundError("User", user_id)
                if is_retired_recovery_admin_password(current["password_hash"]):
                    raise RecoveryAdminProtectedError()
                raise NotFoundError("User", user_id)
            await emit_event(
                conn,
                "auth.account_activated",
                actor_id=actor_id,
                payload={"user_id": user_id},
            )
    return _user_result(row)


async def revoke_user_token(user_id: str, token_id: str, *, actor_id: str) -> dict:
    """Revoke one exact user-owned token and strictly drop its derived role."""
    user_uuid = _uuid(user_id, "user_id")
    token_uuid = _uuid(token_id, "token_id")
    pool = await get_pool()
    needs_cleanup = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked_user_id = await conn.fetchval(
                "SELECT id FROM users WHERE id = $1 FOR UPDATE",
                user_uuid,
            )
            if locked_user_id is None:
                raise NotFoundError("User", user_id)
            deleted = await conn.fetchval(
                "DELETE FROM tokens WHERE id = $1 AND user_id = $2 RETURNING id",
                token_uuid,
                user_uuid,
            )
            cleanup = await conn.fetchrow(
                """
                SELECT completed_at FROM account_token_cleanup
                 WHERE token_id = $1 AND user_id = $2
                """,
                token_uuid,
                user_uuid,
            )
            if deleted is None and cleanup is None:
                raise NotFoundError("Token", token_id)
            if deleted is not None:
                await conn.execute(
                    """
                    INSERT INTO account_token_cleanup (token_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT (token_id) DO NOTHING
                    """,
                    token_uuid,
                    user_uuid,
                )
                needs_cleanup = True
                await emit_event(
                    conn,
                    "auth.token_revoked",
                    actor_id=actor_id,
                    payload={"user_id": user_id, "token_id": token_id},
                )
            elif cleanup["completed_at"] is None:
                needs_cleanup = True

    if needs_cleanup:
        await cleanup_token_roles(pool, user_uuid, [token_uuid])
    return {"user_id": user_id, "token_id": token_id, "revoked": True}


async def identify_user_token(raw_token: str, *, actor_id: str) -> dict:
    """Map a presented token to exact ownership without authenticating it.

    Legacy cleanup must also identify expired credentials and credentials owned
    by suspended users. This path therefore performs a fingerprint lookup only;
    it does not update last_used_at or apply the normal authentication filters.
    """
    token = _required(raw_token, "token")
    if len(token) > 4096:
        raise ValidationError("token is too long")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, user_id FROM tokens WHERE token_hash = $1",
                _hash_token(token),
            )
            if row is None:
                raise NotFoundError("Token", "presented credential")
            result = {
                "user_id": str(row["user_id"]),
                "token_id": str(row["id"]),
            }
            await emit_event(
                conn,
                "auth.token_identified",
                actor_id=actor_id,
                payload=result,
            )
            return result
