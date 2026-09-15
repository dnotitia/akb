"""Bounded, opt-in reconciliation of existing Keycloak-owned AKB accounts.

Each tick reads at most 25 identities. Full-population detection takes a sweep,
not one interval, and outages delay detection. Only suspension is automatic.
"""
from __future__ import annotations

import asyncio
import uuid

from app.config import settings
from app.db.postgres import get_pool
from app.repositories.events_repo import emit_event
from app.services._backfill import BackfillRunner
from app.services.account_markers import is_retired_recovery_admin_password
from app.services.account_service import _suspend_user_in_conn, cleanup_token_roles
from app.services.sso_session_epoch import current_sso_session_authority, lock_active_sso_session_epoch
from app.sso.keycloak_admin import ProviderControlError, get_keycloak_provider_control

_cursor: uuid.UUID | None = None
_cursor_issuer: str | None = None
_PAGE_SIZE = 25
_scan_page_size = _PAGE_SIZE
_sweep_error: str | None = None
_last_sweep_error: str | None = None


def _finish_page(rows, page_size: int, error: str | None) -> str | None:
    """Retry failures on the next sweep without starving unrelated identities."""
    global _cursor, _scan_page_size, _sweep_error, _last_sweep_error
    if error is not None:
        _sweep_error = error
        # Isolate failed pages into individual reads on subsequent ticks/sweeps.
        # This also avoids repeatedly exceeding the adapter's whole-page timeout.
        _scan_page_size = 1
    _cursor = rows[-1]["id"] if len(rows) == page_size else None
    if _cursor is None:
        _last_sweep_error = _sweep_error
        _sweep_error = None
        if _last_sweep_error is None:
            _scan_page_size = _PAGE_SIZE
    # A healthy page cannot hide an unresolved failure elsewhere in the sweep.
    return _sweep_error or _last_sweep_error


def _enabled() -> bool:
    return settings.sso_account_sync_enabled and settings.require_auth_mode() == "sso"


async def _record(*, error: str | None, checked: int, suspended: int, clear_error: bool = False) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""INSERT INTO sso_account_sync_state
            (singleton,last_completed_at,last_error_code,checked,suspended)
            VALUES(true,CASE WHEN $1::text IS NULL THEN clock_timestamp() END,$1,$2,$3)
            ON CONFLICT(singleton) DO UPDATE SET
                last_completed_at=COALESCE(EXCLUDED.last_completed_at,sso_account_sync_state.last_completed_at),
                last_error_code=CASE WHEN $4 THEN EXCLUDED.last_error_code
                    ELSE COALESCE(EXCLUDED.last_error_code,sso_account_sync_state.last_error_code) END,
                checked=EXCLUDED.checked,suspended=EXCLUDED.suspended""",
            error, checked, suspended, clear_error)


async def process_once() -> int:
    global _cursor, _cursor_issuer, _scan_page_size, _sweep_error, _last_sweep_error
    if not _enabled():
        return 0
    checked = suspended = 0
    issuer = settings.keycloak_issuer
    if issuer != _cursor_issuer:
        _cursor = None
        _cursor_issuer = issuer
        _scan_page_size = _PAGE_SIZE
        _sweep_error = _last_sweep_error = None
    rows = None
    page_finished = False
    page_size = min(_scan_page_size, _PAGE_SIZE)
    try:
        authority = current_sso_session_authority()
        control = get_keycloak_provider_control()
        if control.config.broker_issuer != issuer:
            raise ProviderControlError("account_sync_authority_mismatch")
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""SELECT e.id,e.user_id,e.subject FROM external_identities e
                JOIN users u ON u.id=e.user_id
                WHERE e.issuer=$1 AND u.auth_provider='keycloak' AND u.account_kind='human'
                    AND u.account_status='active' AND NOT u.is_recovery_admin
                    AND u.password_hash NOT LIKE '!retired-recovery-admin:%'
                    AND ($2::uuid IS NULL OR e.id>$2)
                ORDER BY e.id LIMIT $3""", issuer, _cursor, page_size)
        if not rows:
            error = _finish_page(rows, page_size, None)
            page_finished = True
            await _record(error=error, checked=0, suspended=0, clear_error=error is None)
            return 0
        states = await control.read_managed_account_states(tuple(r["subject"] for r in rows))
        for identity in rows:
            # Settings can be replaced while the management request is in flight.
            if not _enabled() or settings.keycloak_issuer != issuer:
                raise ProviderControlError("account_sync_authority_changed")
            state = states[identity["subject"]]
            checked += 1
            if state == "active":
                continue
            if state not in {"disabled", "missing"}:
                raise ProviderControlError("account_sync_user_invalid")
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout='5s'")
                    await lock_active_sso_session_epoch(conn, authority)
                    user = await conn.fetchrow("SELECT * FROM users WHERE id=$1 FOR UPDATE", identity["user_id"])
                    binding = await conn.fetchrow("SELECT * FROM external_identities WHERE id=$1 FOR SHARE", identity["id"])
                    if (user is None or binding is None or binding["user_id"] != identity["user_id"]
                            or binding["issuer"] != issuer or binding["subject"] != identity["subject"]
                            or user["auth_provider"] != "keycloak" or user["account_kind"] != "human"
                            or user["account_status"] != "active" or user["is_recovery_admin"]
                            or is_retired_recovery_admin_password(user["password_hash"])):
                        continue
                    if not _enabled() or settings.keycloak_issuer != issuer:
                        raise ProviderControlError("account_sync_authority_changed")
                    token_ids = await _suspend_user_in_conn(conn, identity["user_id"],
                                                          actor_id="system:sso-account-sync")
                    await emit_event(conn, "auth.external_account_suspended", actor_id="system:sso-account-sync",
                        payload={"user_id": str(identity["user_id"]), "issuer": issuer,
                                 "subject": identity["subject"], "external_state": state})
                suspended += 1
            await cleanup_token_roles(pool, identity["user_id"], token_ids)
        error = _finish_page(rows, page_size, None)
        page_finished = True
        await _record(error=error, checked=checked, suspended=suspended,
                      clear_error=_cursor is None and error is None)
    except Exception as exc:
        code = exc.code if isinstance(exc, ProviderControlError) else type(exc).__name__
        if rows is not None and not page_finished:
            code = _finish_page(rows, page_size, code) or code
        else:
            _sweep_error = code
        await _record(error=code, checked=checked, suspended=suspended)
    # Always use the configured idle cadence, including full pages and errors.
    return 0


async def pending_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""SELECT last_completed_at,last_error_code,checked,suspended,
            last_completed_at>clock_timestamp()-make_interval(secs=>$1) AS fresh
            FROM sso_account_sync_state WHERE singleton""", settings.sso_account_sync_interval_secs * 3 + 20)
    result = dict(row) if row else {}
    return {"enabled": _enabled(), "interval_secs": settings.sso_account_sync_interval_secs,
            "page_size": _PAGE_SIZE, **result,
            "ready": bool(_enabled() and result.get("fresh") and result.get("last_error_code") is None)}


_runner = BackfillRunner("sso_account_sync", process_once)


def start() -> None:
    if not _enabled():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _runner.configure_idle_secs(settings.sso_account_sync_interval_secs)
    _runner.start()


stop = _runner.stop
