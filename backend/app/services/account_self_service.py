"""Identity-bound local self-service. Owned vaults never enter this delete path."""
from __future__ import annotations

import uuid

import asyncpg

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, ConflictError, RecoveryAdminProtectedError
from app.repositories.events_repo import emit_event
from app.services.account_markers import is_retired_recovery_admin_password
from app.services.auth_service import AuthenticatedUser, _revoke_sessions_in_conn, verify_password_async

_ADMIN_PREDICATE = """is_admin AND account_status='active' AND account_kind='human'
    AND auth_provider='local' AND NOT is_recovery_admin
    AND password_hash NOT LIKE '!retired-recovery-admin:%'"""


def _identity(user: AuthenticatedUser, expected_user_id: uuid.UUID) -> uuid.UUID:
    uid = uuid.UUID(user.user_id)
    if uid != expected_user_id:
        raise ConflictError("The signed-in account changed. Review this account again.", code="account_identity_changed")
    return uid


def _carrier_reason(user: AuthenticatedUser) -> str | None:
    if not settings.local_human_auth_enabled:
        return "managed_account"
    if user.auth_method != "jwt" or user.account_kind != "human":
        return "human_session_required"
    if not settings.account_self_service_enabled:
        return "rollout_not_enabled"
    return None


def _require_carrier(user: AuthenticatedUser) -> None:
    reason = _carrier_reason(user)
    if reason:
        raise AKBError("Account self-service is unavailable for this session.", 403, code=reason)


def _session_reason(user: AuthenticatedUser) -> str | None:
    if settings.local_human_auth_enabled:
        return _carrier_reason(user)
    if user.auth_method != "browser_session" or user.account_kind != "human":
        return "human_session_required"
    if not settings.account_self_service_enabled:
        return "rollout_not_enabled"
    return None


async def _worker_ready(conn) -> bool:
    return bool(await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM account_deletion_worker_state
        WHERE singleton AND last_seen_at > clock_timestamp()-interval '60 seconds')"""))


async def _read_user(conn, uid: uuid.UUID, *, lock: bool = False):
    row = await conn.fetchrow("SELECT * FROM users WHERE id=$1" + (" FOR UPDATE" if lock else ""), uid)
    if row is None or row["account_status"] != "active":
        raise AKBError("Session is no longer valid.", 401, code="account_session_expired")
    return row


def _require_local_row(row) -> None:
    if row["auth_provider"] != "local" or row["account_kind"] != "human":
        raise AKBError("This account is managed externally.", 403, code="managed_account")
    if row["credential_change_required"]:
        raise AKBError("Change your issued password first.", 403, code="credential_change_required")


async def _blockers(conn, row, *, locked_admin_ids: list[uuid.UUID] | None = None) -> list[dict]:
    result = []
    if row["is_recovery_admin"] or is_retired_recovery_admin_password(row["password_hash"]):
        result.append({"code": "recovery_admin_protected"})
    count = await conn.fetchval("SELECT count(*) FROM vaults WHERE owner_id=$1", row["id"])
    if count:
        result.append({"code": "owned_vaults", "count": count})
    if row["is_admin"]:
        others = await conn.fetchval(
            f"SELECT count(*) FROM users WHERE {_ADMIN_PREDICATE} AND id<>$1"
            " AND ($2::uuid[] IS NULL OR id=ANY($2))", row["id"], locked_admin_ids)
        if not others:
            result.append({"code": "last_active_admin"})
    return result


async def lifecycle(user: AuthenticatedUser) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            row = await _read_user(conn, uuid.UUID(user.user_id))
            reason = _session_reason(user)
            expected_provider = "local" if settings.local_human_auth_enabled else "keycloak"
            if not reason and (row["auth_provider"] != expected_provider or row["account_kind"] != "human"):
                reason = "managed_account"
            deletion_reason = ("managed_account" if not settings.local_human_auth_enabled else
                               reason or (None if await _worker_ready(conn) else "cleanup_unavailable"))
            blockers = await _blockers(conn, row) if not deletion_reason else []
            effects = {
                "active_pats_revoked": await conn.fetchval("""SELECT count(*) FROM tokens WHERE user_id=$1
                    AND key_class='pat' AND (expires_at IS NULL OR expires_at>now())""", row["id"]),
                "shared_publications_preserved": await conn.fetchval("""SELECT count(*) FROM publications p
                    JOIN vaults v ON v.id=p.vault_id WHERE p.created_by=$1 AND v.owner_id IS DISTINCT FROM $1""", row["id"]),
            }
    return {
        "schema_version": 1, "user_id": str(row["id"]), "username": row["username"],
        "revoke_sessions": {"supported": reason is None,
                            "scope": "local_sessions" if settings.local_human_auth_enabled else "sso_browser_sessions",
                            "includes_current": True,
                            "affects_pats": False, "reason": reason},
        "deletion": {"supported": deletion_reason is None, "allowed": deletion_reason is None and not blockers,
                     "reason": deletion_reason, "confirmation": "username_and_current_password",
                     "blockers": blockers, "effects": effects},
    }


async def deletion_blockers(user: AuthenticatedUser, *, cursor: uuid.UUID | None, limit: int) -> dict:
    pool = await get_pool()
    uid = uuid.UUID(user.user_id)
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            total = await conn.fetchval("SELECT count(*) FROM vaults WHERE owner_id=$1", uid)
            rows = await conn.fetch("""SELECT id,name FROM vaults WHERE owner_id=$1
                AND ($2::uuid IS NULL OR id>$2) ORDER BY id LIMIT $3""", uid, cursor, limit+1)
    return {"user_id": str(uid), "total": total,
            "owned_vaults": [{"id": str(r["id"]), "name": r["name"]} for r in rows[:limit]],
            "next_cursor": str(rows[limit-1]["id"]) if len(rows)>limit else None}


async def revoke_sessions(user: AuthenticatedUser, expected_user_id: uuid.UUID) -> dict:
    uid = _identity(user, expected_user_id)
    if not settings.local_human_auth_enabled:
        reason = _session_reason(user)
        if reason:
            raise AKBError("Account session management is unavailable.", 403, code=reason)
        from app.services.sso_browser_session_service import revoke_all_sso_browser_sessions
        cutoff = await revoke_all_sso_browser_sessions(user)
        # Do not clear cookies here: a delayed Set-Cookie could erase a newer
        # login in another tab. Revoked handles are inert; the next login replaces them.
        return {"user_id": str(uid), "revoked_before": cutoff.isoformat()}
    _require_carrier(user)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _read_user(conn, uid, lock=True)
            _require_local_row(row)
            cutoff = await _revoke_sessions_in_conn(conn, uid, actor_id=str(uid), reason="self")
    return {"user_id": str(uid), "revoked_before": cutoff.isoformat()}


async def _password_attempt(conn, uid: uuid.UUID) -> None:
    # Committed separately: failed authentication must not roll its own budget back.
    attempts = await conn.fetchval("""INSERT INTO account_lifecycle_attempts(user_id,window_start,attempts)
        VALUES($1,clock_timestamp(),1) ON CONFLICT(user_id) DO UPDATE SET
        window_start=CASE WHEN account_lifecycle_attempts.window_start<clock_timestamp()-interval '1 minute'
            THEN clock_timestamp() ELSE account_lifecycle_attempts.window_start END,
        attempts=CASE WHEN account_lifecycle_attempts.window_start<clock_timestamp()-interval '1 minute'
            THEN 1 ELSE account_lifecycle_attempts.attempts+1 END RETURNING attempts""", uid)
    if attempts > 5:
        raise AKBError("Too many confirmation attempts. Try again in a minute.", 429, code="reauthentication_rate_limited")


async def delete_account(
    user: AuthenticatedUser, expected_user_id: uuid.UUID, confirm_username: str, current_password: str,
) -> dict:
    uid = _identity(user, expected_user_id)
    _require_carrier(user)
    pool = await get_pool()
    async with pool.acquire() as conn:
        snapshot = await _read_user(conn, uid)
        _require_local_row(snapshot)
        if snapshot["username"] != confirm_username:
            raise ConflictError("Account name does not match.", code="account_identity_changed")
        await _password_attempt(conn, uid)
    if not await verify_password_async(current_password, snapshot["password_hash"]):
        raise AKBError("Current password is incorrect.", 403, code="reauthentication_failed")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Bound lock waits and roll back all effects on ownership/administrator races.
                await conn.execute("SET LOCAL lock_timeout='5s'")
                # Existing admin writers already lock the affected user row. Lock all current
                # eligible admins in UUID order, then count afresh: no second global lock is
                # required in every admin/SSO writer, and a deadlock aborts rather than bypasses.
                admins = await conn.fetch(f"SELECT id FROM users WHERE {_ADMIN_PREDICATE} ORDER BY id FOR UPDATE")
                row = await _read_user(conn, uid, lock=True)
                _require_local_row(row)
                if row["username"] != confirm_username:
                    raise ConflictError("The account changed. Review it again.", code="account_identity_changed")
                if row["password_hash"] != snapshot["password_hash"]:
                    raise AKBError("Password changed. Confirm it again.", 403, code="reauthentication_failed")
                if not await _worker_ready(conn):
                    raise AKBError("Account cleanup is temporarily unavailable.", 503, code="cleanup_unavailable")
                blockers = await _blockers(conn, row, locked_admin_ids=[r["id"] for r in admins])
                if any(b["code"] == "recovery_admin_protected" for b in blockers):
                    raise RecoveryAdminProtectedError()
                if blockers:
                    raise ConflictError("Resolve the account deletion blockers first.",
                                        code="account_deletion_blocked", details={"blockers": blockers})
                await conn.execute("""INSERT INTO account_deletion_cleanup(role_kind,resource_id)
                    SELECT 'token',id FROM tokens WHERE user_id=$1 ON CONFLICT DO NOTHING""", uid)
                await conn.execute("""INSERT INTO account_deletion_cleanup(role_kind,resource_id)
                    VALUES('user',$1) ON CONFLICT DO NOTHING""", uid)
                await conn.execute("UPDATE vault_access SET granted_by=NULL WHERE granted_by=$1", uid)
                await conn.execute("UPDATE vault_access_contributions SET granted_by=NULL WHERE granted_by=$1", uid)
                await conn.execute("UPDATE publications SET created_by=NULL WHERE created_by=$1", uid)
                await emit_event(conn, "auth.account_deleted", actor_id=str(uid), payload={"user_id": str(uid)})
                await conn.execute("DELETE FROM users WHERE id=$1", uid)
    except (asyncpg.DeadlockDetectedError, asyncpg.LockNotAvailableError, asyncpg.ForeignKeyViolationError):
        raise ConflictError("Account resources changed. Review the account again.", code="account_state_changed") from None
    return {"deleted": True, "user_id": str(uid)}
