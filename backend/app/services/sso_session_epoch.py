"""Runtime authority generation for all SSO browser sessions.

The configured UUID is intentionally not a credential. It is an installation-
owned generation marker: normal SSO restarts reuse it, while an explicit epoch
change invalidates every ordinary/admin browser handle and logout fence. AKB
also persists the last observed auth mode, so a real sso -> local -> sso
transition cannot revive rows that were merely ignored while local mode ran.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AuthenticationError
from app.repositories.events_repo import emit_event


@dataclass(frozen=True, slots=True)
class SsoSessionEpochReconcileResult:
    changed: bool
    auth_mode_changed: bool
    epoch_changed: bool
    ordinary_sessions_revoked: int
    admin_sessions_revoked: int
    logout_fences_revoked: int


def current_sso_session_epoch() -> uuid.UUID:
    """Return the validated current epoch or fail closed at request time."""
    value = settings.sso_session_epoch
    if not isinstance(value, uuid.UUID):
        raise AuthenticationError("SSO session authority is unavailable")
    return value


async def lock_active_sso_session_epoch(
    conn,
    expected_epoch: uuid.UUID,
) -> None:
    """Pin one request to the database-observed active SSO generation.

    The share lock serializes with startup reconciliation's row update. A
    draining replica with stale mode/config therefore fails before touching
    browser-session state, while the FK remains the database-level backstop.
    """
    active = await conn.fetchval(
        """
        SELECT sso_session_epoch
          FROM auth_runtime_state
         WHERE singleton = TRUE
           AND auth_mode = 'sso'
           AND sso_session_epoch = $1
         FOR SHARE
        """,
        expected_epoch,
    )
    if active != expected_epoch:
        raise AuthenticationError("SSO session authority is unavailable")


def _command_count(result: object) -> int:
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


async def reconcile_sso_session_epoch() -> SsoSessionEpochReconcileResult:
    """Atomically observe the runtime boundary and revoke stale SSO state."""
    mode = settings.require_auth_mode()
    desired_epoch = current_sso_session_epoch() if mode == "sso" else None
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            insert_result = await conn.execute(
                """
                INSERT INTO auth_runtime_state (
                    singleton, auth_mode, sso_session_epoch
                ) VALUES (TRUE, $1, $2)
                ON CONFLICT (singleton) DO NOTHING
                """,
                mode,
                desired_epoch,
            )
            inserted = _command_count(insert_result) == 1
            observed = await conn.fetchrow(
                """
                SELECT auth_mode, sso_session_epoch
                  FROM auth_runtime_state
                 WHERE singleton = TRUE
                 FOR UPDATE
                """
            )
            if observed is None:
                raise RuntimeError("auth runtime state is unavailable")

            auth_mode_changed = not inserted and observed["auth_mode"] != mode
            epoch_changed = (
                not inserted and observed["sso_session_epoch"] != desired_epoch
            )
            changed = inserted or auth_mode_changed or epoch_changed
            if not changed:
                return SsoSessionEpochReconcileResult(
                    changed=False,
                    auth_mode_changed=False,
                    epoch_changed=False,
                    ordinary_sessions_revoked=0,
                    admin_sessions_revoked=0,
                    logout_fences_revoked=0,
                )

            ordinary = _command_count(
                await conn.execute("DELETE FROM sso_browser_sessions")
            )
            admin = _command_count(
                await conn.execute("DELETE FROM admin_browser_sessions")
            )
            fences = _command_count(
                await conn.execute("DELETE FROM sso_browser_logout_fences")
            )
            if not inserted:
                await conn.execute(
                    """
                    UPDATE auth_runtime_state
                       SET auth_mode = $1,
                           sso_session_epoch = $2,
                           updated_at = NOW()
                     WHERE singleton = TRUE
                    """,
                    mode,
                    desired_epoch,
                )
            await emit_event(
                conn,
                "auth.runtime_boundary_changed",
                actor_id="system",
                payload={
                    "auth_mode": mode,
                    "initialized": inserted,
                    "auth_mode_changed": auth_mode_changed,
                    "sso_session_epoch_changed": epoch_changed,
                    "ordinary_sessions_revoked": ordinary,
                    "admin_sessions_revoked": admin,
                    "logout_fences_revoked": fences,
                },
            )
    return SsoSessionEpochReconcileResult(
        changed=True,
        auth_mode_changed=auth_mode_changed,
        epoch_changed=epoch_changed,
        ordinary_sessions_revoked=ordinary,
        admin_sessions_revoked=admin,
        logout_fences_revoked=fences,
    )
