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
from app.exceptions import NotFoundError
from app.repositories.events_repo import emit_event
from app.services.auth_service import hash_password


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
    method: Literal["admin_ui", "cli"],
) -> tuple[str, str]:
    """Generate a temp password, replace the user's password_hash, emit audit.

    Returns (temp_password, username). `actor_id` is None for CLI invocations
    (no authenticated principal); audit event carries `method` to distinguish.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, username FROM users WHERE username = $1",
                username,
            )
            if row is None:
                raise NotFoundError("User", username)

            temp = generate_temp_password()
            await conn.execute(
                "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
                hash_password(temp),
                row["id"],
            )
            await emit_event(
                conn,
                "auth.password_reset",
                ref_type="user",
                ref_id=str(row["id"]),
                actor_id=actor_id,
                payload={
                    "user_id": str(row["id"]),
                    "username": row["username"],
                    "method": method,
                },
            )
    return temp, row["username"]
