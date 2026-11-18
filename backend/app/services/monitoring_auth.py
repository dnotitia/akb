import hashlib
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AuthenticationError, ForbiddenError, NotFoundError
from app.repositories.events_repo import emit_event
from app.services.auth_service import verify_local_login
from app.services.auth_policy import require_local_auth_enabled
from app.services.sso_browser_session_service import lock_sso_sid_and_check_logout
from app.services.sso_session_epoch import (
    SsoSessionAuthority, current_sso_session_authority, lock_active_sso_session_epoch,
)


SESSION_TTL = timedelta(minutes=30)
_TOKEN = re.compile(r"akb_monitor_[A-Za-z0-9_-]{43}")


@dataclass(frozen=True, slots=True)
class MonitoringSession:
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MonitoringSsoProof:
    issuer: str
    subject: str
    sid: str
    issued_at: datetime
    expires_at: datetime

    @classmethod
    def from_verified_tokens(
        cls, access: Mapping[str, object], identity: Mapping[str, object],
    ) -> "MonitoringSsoProof":
        issuer, subject, sid = (access.get(key) for key in ("iss", "sub", "sid"))
        if (
            not isinstance(issuer, str) or issuer != settings.keycloak_issuer
            or not isinstance(subject, str) or not 0 < len(subject) <= 1024
            or not isinstance(sid, str) or not 0 < len(sid) <= 255
            or identity.get("iss") != issuer or identity.get("sub") != subject or identity.get("sid") != sid
        ):
            raise AuthenticationError()
        issued_at, expires_at, id_expires_at = access.get("iat"), access.get("exp"), identity.get("exp")
        if type(issued_at) is not int or type(expires_at) is not int or type(id_expires_at) is not int:
            raise AuthenticationError()
        try:
            return cls(
                issuer, subject, sid, datetime.fromtimestamp(issued_at, UTC),
                datetime.fromtimestamp(min(expires_at, id_expires_at), UTC),
            )
        except (OverflowError, OSError, ValueError):
            raise AuthenticationError() from None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def set_monitoring_access(user_id: UUID, enabled: bool, *, actor_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT id, is_admin, is_recovery_admin, account_kind, account_status,
                   monitoring_access, credential_change_required
              FROM users WHERE id = $1 FOR UPDATE
            """,
            user_id,
        )
        if row is None:
            raise NotFoundError("User", str(user_id))
        if row["monitoring_access"] is enabled:
            return
        if not enabled and row["monitoring_access"] is None:
            raise ForbiddenError("The account is not dedicated to monitoring")
        if enabled and (
            row["is_admin"] or row["is_recovery_admin"] or row["account_kind"] != "human"
            or row["account_status"] != "active" or row["credential_change_required"]
            or await conn.fetchval("SELECT EXISTS (SELECT 1 FROM tokens WHERE user_id = $1)", user_id)
        ):
            raise ForbiddenError("Monitoring requires an active non-administrator human account without API keys")
        await conn.execute(
            """
            UPDATE users SET monitoring_access = $2, session_generation = session_generation + 1,
                             updated_at = now()
             WHERE id = $1
            """,
            user_id, enabled,
        )
        await conn.execute("DELETE FROM mcp_monitoring.sessions WHERE user_id = $1", user_id)
        await emit_event(
            conn, "auth.monitoring_access_changed", actor_id=actor_id,
            payload={"user_id": str(user_id), "enabled": enabled},
        )


async def _issue(conn, user, *, proof: MonitoringSsoProof | None = None) -> MonitoringSession:
    now = datetime.now(UTC)
    expiry = min(now + SESSION_TTL, proof.expires_at) if proof else now + SESSION_TTL
    if expiry <= now or user["credential_change_required"]:
        raise AuthenticationError()
    token = "akb_monitor_" + secrets.token_urlsafe(32)
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"monitoring-session:{user['id']}",
    )
    await conn.execute("DELETE FROM mcp_monitoring.sessions WHERE expires_at <= now()")
    await conn.execute(
        """
        DELETE FROM mcp_monitoring.sessions WHERE token_hash IN (
            SELECT token_hash FROM mcp_monitoring.sessions WHERE user_id = $1
            ORDER BY expires_at DESC, token_hash OFFSET 7
        )
        """,
        user["id"],
    )
    await conn.execute(
        """
        INSERT INTO mcp_monitoring.sessions (
            token_hash, user_id, session_generation, runtime_generation, auth_mode,
            external_identity_id, identity_subject, keycloak_sid, expires_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """,
        _hash(token), user["id"], user["session_generation"], settings.auth_runtime_generation,
        "sso" if proof else "local", user["external_identity_id"] if proof else None,
        proof.subject if proof else None, proof.sid if proof else None, expiry,
    )
    return MonitoringSession(token, expiry)


async def login_monitoring_local(username: str, password: str) -> MonitoringSession:
    require_local_auth_enabled()
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _lock_local_runtime(conn)
        user = await verify_local_login(conn, username, password, monitoring=True)
        return await _issue(conn, user)


def _monitoring_sso_authority() -> SsoSessionAuthority:
    if not settings.keycloak_enabled or settings.keycloak_enrollment_mode == "disabled":
        raise AuthenticationError()
    return current_sso_session_authority()


async def login_monitoring_sso(proof: MonitoringSsoProof) -> MonitoringSession:
    authority = _monitoring_sso_authority()
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await lock_active_sso_session_epoch(conn, authority)
        await lock_sso_sid_and_check_logout(
            conn, authority.session_epoch, proof.issuer, proof.sid, proof.subject, proof.issued_at,
        )
        user = await conn.fetchrow(
            """
            SELECT u.id, u.session_generation, u.credential_change_required, e.id AS external_identity_id
              FROM external_identities e JOIN users u ON u.id = e.user_id
             WHERE e.issuer = $1 AND e.subject = $2
               AND u.monitoring_access IS TRUE AND u.account_status = 'active' AND u.auth_provider = 'keycloak'
             FOR SHARE OF u, e
            """,
            proof.issuer, proof.subject,
        )
        if user is None:
            raise AuthenticationError()
        return await _issue(conn, user, proof=proof)


async def _lock_local_runtime(conn) -> None:
    live = await conn.fetchval(
        """
        SELECT TRUE FROM auth_runtime_state WHERE singleton
          AND auth_mode = 'local' AND runtime_generation = $1 FOR SHARE
        """,
        settings.auth_runtime_generation,
    )
    if live is not True:
        raise AuthenticationError()


async def require_monitoring_session(token: str) -> None:
    if not _TOKEN.fullmatch(token):
        raise AuthenticationError()
    mode = settings.require_auth_mode()
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        authority = _monitoring_sso_authority() if mode == "sso" else None
        if authority:
            await lock_active_sso_session_epoch(conn, authority)
        else:
            await _lock_local_runtime(conn)
        live = await conn.fetchval(
            """
            SELECT TRUE FROM mcp_monitoring.sessions s JOIN users u ON u.id = s.user_id
             WHERE s.token_hash = $1 AND s.auth_mode = $2 AND s.runtime_generation = $3
               AND s.expires_at > now()
               AND u.monitoring_access IS TRUE AND u.account_status = 'active' AND NOT u.credential_change_required
               AND u.session_generation = s.session_generation
               AND (
                    ($2 = 'local' AND u.auth_provider = 'local')
                    OR (
                        $2 = 'sso' AND u.auth_provider = 'keycloak'
                        AND EXISTS (
                            SELECT 1 FROM external_identities e
                             WHERE e.id = s.external_identity_id AND e.user_id = u.id
                               AND e.issuer = $4 AND e.subject = s.identity_subject
                        )
                    )
               )
            """,
            _hash(token), mode, settings.auth_runtime_generation,
            settings.keycloak_issuer,
        )
    if live is not True:
        raise AuthenticationError()


async def end_monitoring_session(token: str) -> None:
    if _TOKEN.fullmatch(token):
        pool = await get_pool()
        await pool.execute("DELETE FROM mcp_monitoring.sessions WHERE token_hash = $1", _hash(token))
