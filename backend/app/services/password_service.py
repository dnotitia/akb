"""Password reset (admin/CLI-mediated).

Used by:
  - REST POST /admin/users/{user_id}/reset-password (admin auth)
  - CLI `python -m app.cli reset-password <username>` (shell access)

Both call reset_password() and surface the returned temp password to the
caller. The caller is responsible for getting that password to the user
out-of-band (Slack DM, in person, etc.). The temp password is bcrypt-hashed
into users.password_hash and is never persisted in plaintext.
"""
from __future__ import annotations

import secrets
from typing import Literal

from app.db.postgres import get_pool
from app.exceptions import (
    AccountSuspendedError,
    NotFoundError,
    PasswordLifecycleUnavailableError,
)
from app.repositories.events_repo import emit_event
from app.services.auth_policy import require_local_auth_enabled
from app.services.auth_service import (
    REVOKE_REASON_PASSWORD_RESET,
    _revoke_sessions_in_conn,
    hash_password_async,
)


def generate_temp_password() -> str:
    """12-char URL-safe random password with dash grouping for readability.

    secrets.token_urlsafe(9) -> 12 base64url chars -> ~50 bits entropy.
    Dash-grouped 4-4-4 so the admin can read it aloud or copy-paste
    without breaking on selection.
    """
    raw = secrets.token_urlsafe(9)[:12]
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


async def reset_password(
    *,
    username: str,
    actor_id: str | None,
    method: Literal[
        "admin_ui",
        "cli",
        # Recovery-administrator credential issue, which delegates here rather
        # than reimplementing the reset. The discriminator keeps the two
        # authorities apart in the audit trail: an independent service
        # administrator token, or workspace shell access.
        "recovery_admin_api",
        "recovery_admin_cli",
    ],
) -> tuple[str, str]:
    """Generate a temp password, replace the user's password_hash, emit audit.

    The account is left owing a credential change, so the delivered value
    stops being a working password the moment its holder signs in with it —
    the local equivalent of the identity provider's ``temporary`` credential
    plus ``UPDATE_PASSWORD`` required action.

    Returns (temp_password, username). `actor_id` is None for CLI invocations
    (no authenticated principal); audit event carries `method` to distinguish.
    """
    require_local_auth_enabled()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, username, auth_provider, account_status, account_kind
                  FROM users WHERE username = $1
                   FOR UPDATE
                """,
                username,
            )
            if row is None:
                raise NotFoundError("User", username)
            if row["account_status"] != "active":
                raise AccountSuspendedError()
            if row["auth_provider"] != "local" or row["account_kind"] != "human":
                raise PasswordLifecycleUnavailableError()

            temp = generate_temp_password()
            # The returned value is delivered to a person out-of-band, which
            # is what makes it temporary: the marker is set in the same
            # statement that installs the hash, so the account cannot be
            # observed holding an issued credential without owing a
            # replacement for it. change_password clears it.
            #
            # Only the sessions this credential can produce are limited.
            # Personal Access Tokens the account already holds keep working,
            # deliberately and consistently with the session revoke below:
            # a PAT is not the credential that was just handed over, and
            # breaking stored integrations on every administrative reset is
            # a different (and larger) change than this one.
            await conn.execute(
                """
                UPDATE users
                   SET password_hash = $1,
                       credential_change_required = true,
                       updated_at = NOW()
                 WHERE id = $2
                """,
                await hash_password_async(temp),
                row["id"],
            )
            # Otherwise the attacker who triggered the reset (or held
            # the prior password) keeps any JWT they hold valid.
            await _revoke_sessions_in_conn(
                conn,
                row["id"],
                actor_id=actor_id or str(row["id"]),
                reason=REVOKE_REASON_PASSWORD_RESET,
            )
            await emit_event(
                conn,
                "auth.password_reset",
                resource_uri=None,
                actor_id=actor_id,
                payload={
                    "user_id": str(row["id"]),
                    "username": row["username"],
                    "method": method,
                },
            )
    return temp, row["username"]
