"""Monotonic runtime authority for all SSO browser sessions.

``auth_runtime_generation`` is installation-owned and monotonic. An ordinary
restart must present the exact stored generation/mode/epoch tuple. A deliberate
transition presents a greater generation; stale or conflicting replicas fail
closed and can never restore an older authority boundary.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import asyncpg

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AuthenticationError
from app.repositories.events_repo import emit_event


@dataclass(frozen=True, slots=True)
class AuthRuntimeBoundary:
    runtime_generation: int
    auth_mode: str
    sso_session_epoch: uuid.UUID | None

    def __post_init__(self) -> None:
        valid_generation = type(self.runtime_generation) is int and self.runtime_generation > 0
        valid_shape = (self.auth_mode == "local" and self.sso_session_epoch is None) or (
            self.auth_mode == "sso" and isinstance(self.sso_session_epoch, uuid.UUID)
        )
        if not valid_generation or not valid_shape:
            raise RuntimeError("Invalid authentication runtime boundary")


@dataclass(frozen=True, slots=True)
class SsoSessionAuthority:
    runtime_generation: int
    session_epoch: uuid.UUID


@dataclass(frozen=True, slots=True)
class SsoSessionEpochReconcileResult:
    changed: bool
    auth_mode_changed: bool
    epoch_changed: bool
    ordinary_sessions_revoked: int
    admin_sessions_revoked: int
    logout_fences_revoked: int


def configured_auth_runtime_boundary() -> AuthRuntimeBoundary:
    mode = settings.require_auth_mode()
    epoch = current_sso_session_epoch() if mode == "sso" else None
    return AuthRuntimeBoundary(settings.auth_runtime_generation, mode, epoch)


def current_sso_session_epoch() -> uuid.UUID:
    """Return the configured epoch or fail closed without disclosing it."""
    value = settings.sso_session_epoch
    if not isinstance(value, uuid.UUID):
        raise AuthenticationError("SSO session authority is unavailable")
    return value


def current_sso_session_authority() -> SsoSessionAuthority:
    """Snapshot the exact configured generation/epoch request boundary."""
    if settings.require_auth_mode() != "sso":
        raise AuthenticationError("SSO session authority is unavailable")
    generation = settings.auth_runtime_generation
    epoch = current_sso_session_epoch()
    if type(generation) is not int or generation <= 0:
        raise AuthenticationError("SSO session authority is unavailable")
    return SsoSessionAuthority(generation, epoch)


async def lock_active_sso_session_epoch(
    conn,
    authority: SsoSessionAuthority,
) -> None:
    """Pin one request to the exact database-observed SSO authority.

    Every browser operation takes this row lock before touching a session
    relation. Reconciliation takes the same authority-first order, so a stale
    replica either finishes before the transition purge or fails afterward.
    """
    active = await conn.fetchval(
        """
        SELECT TRUE
          FROM auth_runtime_state
         WHERE singleton = TRUE
           AND runtime_generation = $1
           AND auth_mode = 'sso'
           AND sso_session_epoch = $2
         FOR SHARE
        """,
        authority.runtime_generation,
        authority.session_epoch,
    )
    if active is not True:
        raise AuthenticationError("SSO session authority is unavailable")


def _command_count(result: object) -> int:
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


async def _lock_session_relations(conn, mode: str) -> None:
    # Keep this exact order aligned with migration 076. The auth relation and
    # authority row are always acquired by the caller first.
    await conn.execute(f"LOCK TABLE admin_browser_sessions IN {mode} MODE")
    await conn.execute(f"LOCK TABLE sso_browser_sessions IN {mode} MODE")
    await conn.execute(f"LOCK TABLE sso_browser_logout_fences IN {mode} MODE")


async def _reconcile_once(
    boundary: AuthRuntimeBoundary,
    *,
    upgrade_preflight: bool,
) -> SsoSessionEpochReconcileResult:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Relation and row order: authority relation -> authority row ->
            # admin sessions -> ordinary sessions -> logout fences.
            await conn.execute("LOCK TABLE auth_runtime_state IN SHARE ROW EXCLUSIVE MODE")
            observed = await conn.fetchrow(
                """
                SELECT runtime_generation, auth_mode, sso_session_epoch
                  FROM auth_runtime_state
                 WHERE singleton = TRUE
                 FOR UPDATE
                """
            )
            upgrade = await conn.fetchrow(
                """
                SELECT state, runtime_generation_floor
                  FROM auth_runtime_epoch_upgrade
                 WHERE singleton = TRUE
                """
            )
            if upgrade is None:
                raise RuntimeError("Authentication runtime upgrade state is unavailable")
            upgrade_state = upgrade["state"]
            generation_floor = upgrade["runtime_generation_floor"]
            if upgrade_state in {"required", "rollback_ready"} and (
                settings.sso_session_epoch_upgrade != "stop-the-world-v1" or not upgrade_preflight
            ):
                raise RuntimeError(
                    "SSO session epoch upgrade requires the executable stop-the-world-v1 quiescent preflight"
                )

            inserted = observed is None
            if inserted and boundary.runtime_generation <= generation_floor:
                raise RuntimeError("Authentication runtime generation is stale")
            if observed is not None:
                observed_generation = observed["runtime_generation"]
                exact = (
                    observed_generation == boundary.runtime_generation
                    and observed["auth_mode"] == boundary.auth_mode
                    and observed["sso_session_epoch"] == boundary.sso_session_epoch
                )
                if exact and upgrade_state == "enforced":
                    return SsoSessionEpochReconcileResult(
                        changed=False,
                        auth_mode_changed=False,
                        epoch_changed=False,
                        ordinary_sessions_revoked=0,
                        admin_sessions_revoked=0,
                        logout_fences_revoked=0,
                    )
                if boundary.runtime_generation < observed_generation:
                    raise RuntimeError("Authentication runtime generation is stale")
                if boundary.runtime_generation == observed_generation and not exact:
                    raise RuntimeError("Authentication runtime generation conflicts with the active boundary")

            auth_mode_changed = bool(observed is not None and observed["auth_mode"] != boundary.auth_mode)
            epoch_changed = bool(observed is not None and observed["sso_session_epoch"] != boundary.sso_session_epoch)

            if inserted:
                await conn.execute(
                    """
                    INSERT INTO auth_runtime_state (
                        singleton, runtime_generation, auth_mode,
                        sso_session_epoch
                    ) VALUES (TRUE, $1, $2, $3)
                    """,
                    boundary.runtime_generation,
                    boundary.auth_mode,
                    boundary.sso_session_epoch,
                )

            await _lock_session_relations(conn, "SHARE ROW EXCLUSIVE")
            admin = _command_count(await conn.execute("DELETE FROM admin_browser_sessions"))
            ordinary = _command_count(await conn.execute("DELETE FROM sso_browser_sessions"))
            fences = _command_count(await conn.execute("DELETE FROM sso_browser_logout_fences"))
            if not inserted:
                await conn.execute(
                    """
                    UPDATE auth_runtime_state
                       SET runtime_generation = $1,
                           auth_mode = $2,
                           sso_session_epoch = $3,
                           updated_at = NOW()
                     WHERE singleton = TRUE
                    """,
                    boundary.runtime_generation,
                    boundary.auth_mode,
                    boundary.sso_session_epoch,
                )
            await conn.execute(
                """
                UPDATE auth_runtime_epoch_upgrade
                   SET state = 'enforced',
                       runtime_generation_floor = GREATEST(
                           runtime_generation_floor, $1
                       ),
                       updated_at = NOW()
                 WHERE singleton = TRUE
                """,
                boundary.runtime_generation,
            )
            await emit_event(
                conn,
                "auth.runtime_boundary_changed",
                actor_id="system",
                payload={
                    "auth_mode": boundary.auth_mode,
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


async def reconcile_sso_session_epoch(
    boundary: AuthRuntimeBoundary | None = None,
    *,
    upgrade_preflight: bool = False,
) -> SsoSessionEpochReconcileResult:
    """Accept an exact restart or atomically advance one monotonic boundary."""
    desired = boundary or configured_auth_runtime_boundary()
    for attempt in range(3):
        try:
            return await _reconcile_once(
                desired,
                upgrade_preflight=upgrade_preflight,
            )
        except asyncpg.DeadlockDetectedError:
            # PostgreSQL rolled back the whole transaction. Replaying the same
            # immutable desired boundary is therefore safe and idempotent.
            if attempt == 2:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


async def prepare_sso_session_epoch_rollback() -> None:
    """Purge current authority and reopen only the legacy rollback bridge.

    The executable preflight checks that all current backends are stopped before
    calling this transaction. No epoch or generation value is logged or emitted.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("LOCK TABLE auth_runtime_state IN SHARE ROW EXCLUSIVE MODE")
            await conn.fetchrow(
                """
                SELECT runtime_generation
                  FROM auth_runtime_state
                 WHERE singleton = TRUE
                 FOR UPDATE
                """
            )
            await _lock_session_relations(conn, "SHARE ROW EXCLUSIVE")
            await conn.execute("DELETE FROM admin_browser_sessions")
            await conn.execute("DELETE FROM sso_browser_sessions")
            await conn.execute("DELETE FROM sso_browser_logout_fences")
            await conn.execute("DELETE FROM auth_runtime_state")
            await conn.execute(
                """
                UPDATE auth_runtime_epoch_upgrade
                   SET state = 'rollback_ready', updated_at = NOW()
                 WHERE singleton = TRUE
                """
            )
