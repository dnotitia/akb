"""App credential exchange and live control-plane authorization.

App credentials prove a deployment only at the exchange endpoint. App tokens
are short-lived identity carriers and intentionally contain no Vault,
installation, grant, capability, or resource claims.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import jwt

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import (
    AKBError,
    AuthenticationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.services import audit_log

APP_CREDENTIAL_PREFIX = "akb_app_"
APP_TOKEN_TYPE = "AKB-APP"
APP_TOKEN_ISSUER = "akb"
APP_TOKEN_AUDIENCE = "akb-app-control-plane"

# App tokens are structurally limited to control-plane verbs. A grant row may
# contain any string for forward-compatible storage, but strings outside this
# set can never open a runtime path.
SUPPORTED_APP_CAPABILITIES = frozenset(
    {
        "installation:read",
        "inventory:read",
        "rollout:read",
        "rollout:request",
    }
)


@dataclass(frozen=True)
class AppPrincipal:
    app_id: uuid.UUID
    credential_id: uuid.UUID
    credential_generation: int
    deployment: str
    token_id: str
    expires_at: datetime


def _credential_hash(raw_credential: str) -> str:
    return hashlib.sha256(raw_credential.encode()).hexdigest()


def generate_app_credential() -> tuple[str, str, str]:
    raw = APP_CREDENTIAL_PREFIX + secrets.token_urlsafe(32)
    return raw, _credential_hash(raw), raw[:16]


def _configured_app_secret() -> str:
    secret = settings.app_token_secret.strip()
    if not secret:
        raise AKBError(
            "App token exchange is not configured",
            status_code=503,
            code="app_identity_unavailable",
        )
    if secret == settings.system_hmac_secret_effective:
        raise AKBError(
            "App token signing is not safely configured",
            status_code=503,
            code="app_identity_unavailable",
        )
    return secret


def _validate_expiry(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        raise ValidationError("expires_at must include a timezone")
    normalized = expires_at.astimezone(timezone.utc)
    if normalized <= datetime.now(timezone.utc):
        raise ValidationError("expires_at must be in the future")
    return normalized


def _metadata(row: Any) -> dict[str, Any]:
    return {
        "credential_id": str(row["id"]),
        "app_id": str(row["app_id"]),
        "deployment": row["deployment"],
        "prefix": row["credential_prefix"],
        "status": row["status"],
        "generation": row["generation"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "overlap_until": (
            row["overlap_until"].isoformat() if row["overlap_until"] else None
        ),
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        "last_exchanged_at": (
            row["last_exchanged_at"].isoformat()
            if row["last_exchanged_at"]
            else None
        ),
        "created_at": row["created_at"].isoformat(),
    }


def record_app_audit(
    action: str,
    *,
    correlation_id: str,
    outcome: str,
    reason: str,
    actor: str | None = None,
    actor_id: str | None = None,
    app_id: uuid.UUID | str | None = None,
    deployment: str | None = None,
    installation_id: uuid.UUID | str | None = None,
    vault_id: uuid.UUID | str | None = None,
    generation: int | None = None,
) -> None:
    """Write bounded identity metadata only; never accept a proof or token."""
    meta = {
        "correlation_id": correlation_id,
        "result": outcome,
        "reason": reason,
        "app_id": str(app_id) if app_id is not None else None,
        "deployment": deployment,
        "installation_id": (
            str(installation_id) if installation_id is not None else None
        ),
        "vault_id": str(vault_id) if vault_id is not None else None,
        "generation": generation,
    }
    audit_log.record(
        action=action,
        actor=actor,
        actor_id=actor_id,
        target=(f"app_id={app_id}" if app_id is not None else None),
        outcome=outcome,
        code=reason,
        meta=meta,
    )


async def issue_app_credential(
    app_id: uuid.UUID,
    deployment: str,
    *,
    actor: str,
    actor_id: str,
    correlation_id: str,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    deployment = deployment.strip()
    if not deployment:
        raise ValidationError("deployment must not be empty")
    expires_at = _validate_expiry(expires_at)
    raw, proof_hash, prefix = generate_app_credential()
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"{app_id}:{deployment}",
                )
                if not await conn.fetchval(
                    "SELECT 1 FROM app_definitions WHERE id = $1",
                    app_id,
                ):
                    raise NotFoundError("App credential target", "not found")
                generation = await conn.fetchval(
                    """
                    SELECT COALESCE(MAX(generation), 0) + 1
                      FROM app_credentials
                     WHERE app_id = $1 AND deployment = $2
                    """,
                    app_id,
                    deployment,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO app_credentials (
                        app_id, deployment, generation, credential_hash,
                        credential_prefix, expires_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING *
                    """,
                    app_id,
                    deployment,
                    generation,
                    proof_hash,
                    prefix,
                    expires_at,
                )
    except asyncpg.UniqueViolationError:
        record_app_audit(
            "app.credential.issue",
            correlation_id=correlation_id,
            outcome="error",
            reason="active_credential_exists",
            actor=actor,
            actor_id=actor_id,
            app_id=app_id,
            deployment=deployment,
        )
        raise ConflictError("An active credential already exists for this deployment") from None
    except (NotFoundError, ValidationError):
        record_app_audit(
            "app.credential.issue",
            correlation_id=correlation_id,
            outcome="error",
            reason="credential_target_denied",
            actor=actor,
            actor_id=actor_id,
            app_id=app_id,
            deployment=deployment,
        )
        raise

    result = _metadata(row)
    result["credential"] = raw
    result["note"] = "Save this credential — it won't be shown again."
    record_app_audit(
        "app.credential.issue",
        correlation_id=correlation_id,
        outcome="ok",
        reason="issued",
        actor=actor,
        actor_id=actor_id,
        app_id=app_id,
        deployment=deployment,
        generation=row["generation"],
    )
    return result


async def list_app_credentials(
    app_id: uuid.UUID,
    *,
    deployment: str | None = None,
) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
              FROM app_credentials
             WHERE app_id = $1
               AND ($2::text IS NULL OR deployment = $2)
             ORDER BY deployment, generation DESC
            """,
            app_id,
            deployment,
        )
    return [_metadata(row) for row in rows]


async def rotate_app_credential(
    app_id: uuid.UUID,
    credential_id: uuid.UUID,
    *,
    actor: str,
    actor_id: str,
    correlation_id: str,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    expires_at = _validate_expiry(expires_at)
    raw, proof_hash, prefix = generate_app_credential()
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow(
                    """
                    SELECT *
                      FROM app_credentials
                     WHERE id = $1 AND app_id = $2
                     FOR UPDATE
                    """,
                    credential_id,
                    app_id,
                )
                if target is None:
                    raise NotFoundError("App credential", "not found")
                deployment = target["deployment"]
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"{app_id}:{deployment}",
                )
                target = await conn.fetchrow(
                    "SELECT * FROM app_credentials WHERE id = $1 FOR UPDATE",
                    credential_id,
                )
                if target["status"] != "active":
                    raise ConflictError("Only the current active credential can be rotated")

                await conn.execute(
                    """
                    UPDATE app_credentials
                       SET status = 'revoked',
                           overlap_until = NULL,
                           revoked_at = NOW()
                     WHERE app_id = $1
                       AND deployment = $2
                       AND status = 'rotated'
                    """,
                    app_id,
                    deployment,
                )
                await conn.execute(
                    """
                    UPDATE app_credentials
                       SET status = 'rotated',
                           overlap_until = NOW() + ($2 * INTERVAL '1 second')
                     WHERE id = $1
                    """,
                    credential_id,
                    settings.app_credential_overlap_seconds,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO app_credentials (
                        app_id, deployment, generation, credential_hash,
                        credential_prefix, expires_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING *
                    """,
                    app_id,
                    deployment,
                    target["generation"] + 1,
                    proof_hash,
                    prefix,
                    expires_at,
                )
    except (NotFoundError, ConflictError):
        record_app_audit(
            "app.credential.rotate",
            correlation_id=correlation_id,
            outcome="error",
            reason="credential_target_denied",
            actor=actor,
            actor_id=actor_id,
            app_id=app_id,
        )
        raise

    result = _metadata(row)
    result["credential"] = raw
    result["note"] = "Save this credential — it won't be shown again."
    record_app_audit(
        "app.credential.rotate",
        correlation_id=correlation_id,
        outcome="ok",
        reason="rotated",
        actor=actor,
        actor_id=actor_id,
        app_id=app_id,
        deployment=row["deployment"],
        generation=row["generation"],
    )
    return result


async def revoke_app_credential(
    app_id: uuid.UUID,
    credential_id: uuid.UUID,
    *,
    actor: str,
    actor_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT *
                      FROM app_credentials
                     WHERE id = $1 AND app_id = $2
                     FOR UPDATE
                    """,
                    credential_id,
                    app_id,
                )
                if row is None:
                    raise NotFoundError("App credential", "not found")
                if row["status"] != "revoked":
                    row = await conn.fetchrow(
                        """
                        UPDATE app_credentials
                           SET status = 'revoked',
                               overlap_until = NULL,
                               revoked_at = NOW()
                         WHERE id = $1
                        RETURNING *
                        """,
                        credential_id,
                    )
    except NotFoundError:
        record_app_audit(
            "app.credential.revoke",
            correlation_id=correlation_id,
            outcome="error",
            reason="credential_target_denied",
            actor=actor,
            actor_id=actor_id,
            app_id=app_id,
        )
        raise

    record_app_audit(
        "app.credential.revoke",
        correlation_id=correlation_id,
        outcome="ok",
        reason="revoked",
        actor=actor,
        actor_id=actor_id,
        app_id=app_id,
        deployment=row["deployment"],
        generation=row["generation"],
    )
    return _metadata(row)


def _create_app_token(
    *,
    app_id: uuid.UUID,
    credential_id: uuid.UUID,
    generation: int,
) -> tuple[str, datetime, str]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.app_token_ttl_seconds)
    token_id = str(uuid.uuid4())
    payload = {
        "iss": APP_TOKEN_ISSUER,
        "aud": APP_TOKEN_AUDIENCE,
        "sub": str(app_id),
        "cid": str(credential_id),
        "gen": generation,
        "jti": token_id,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(
        payload,
        _configured_app_secret(),
        algorithm="HS256",
        headers={"typ": APP_TOKEN_TYPE},
    )
    return token, expires_at, token_id


def decode_app_token(raw_token: str) -> dict[str, Any] | None:
    try:
        if jwt.get_unverified_header(raw_token).get("typ") != APP_TOKEN_TYPE:
            return None
        return jwt.decode(
            raw_token,
            _configured_app_secret(),
            algorithms=["HS256"],
            audience=APP_TOKEN_AUDIENCE,
            issuer=APP_TOKEN_ISSUER,
            options={
                "require": ["sub", "cid", "gen", "jti", "iat", "exp", "aud", "iss"],
            },
        )
    except (AKBError, jwt.InvalidTokenError, ValueError):
        return None


async def exchange_app_credential(
    raw_credential: str,
    *,
    correlation_id: str,
) -> dict[str, Any]:
    try:
        _configured_app_secret()
    except AKBError:
        record_app_audit(
            "app.credential.exchange",
            correlation_id=correlation_id,
            outcome="error",
            reason="app_identity_unavailable",
        )
        raise

    if (
        not raw_credential.startswith(APP_CREDENTIAL_PREFIX)
        or len(raw_credential) > 512
    ):
        record_app_audit(
            "app.credential.exchange",
            correlation_id=correlation_id,
            outcome="error",
            reason="invalid_app_credential",
        )
        raise AuthenticationError()

    proof_hash = _credential_hash(raw_credential)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH proof AS (
                SELECT *
                  FROM app_credentials
                 WHERE credential_hash = $1
                   AND (expires_at IS NULL OR expires_at > NOW())
            ),
            current_credential AS (
                SELECT active.*
                  FROM app_credentials AS active
                  JOIN proof
                    ON proof.app_id = active.app_id
                   AND proof.deployment = active.deployment
                 WHERE active.status = 'active'
                   AND (active.expires_at IS NULL OR active.expires_at > NOW())
                   AND (
                       proof.id = active.id
                       OR (
                           proof.status = 'rotated'
                           AND proof.overlap_until > NOW()
                           AND proof.generation + 1 = active.generation
                       )
                   )
            ),
            touched AS (
                UPDATE app_credentials AS used
                   SET last_exchanged_at = NOW()
                  FROM proof, current_credential
                 WHERE used.id = proof.id
                RETURNING used.id
            )
            SELECT current_credential.*, touched.id AS proof_id
              FROM current_credential, touched
            """,
            proof_hash,
        )
    if row is None:
        record_app_audit(
            "app.credential.exchange",
            correlation_id=correlation_id,
            outcome="error",
            reason="invalid_app_credential",
        )
        raise AuthenticationError()

    token, expires_at, _token_id = _create_app_token(
        app_id=row["app_id"],
        credential_id=row["id"],
        generation=row["generation"],
    )
    record_app_audit(
        "app.credential.exchange",
        correlation_id=correlation_id,
        outcome="ok",
        reason="exchanged",
        actor=f"app:{row['app_id']}",
        actor_id=str(row["app_id"]),
        app_id=row["app_id"],
        deployment=row["deployment"],
        generation=row["generation"],
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": settings.app_token_ttl_seconds,
        "expires_at": expires_at.isoformat(),
        "correlation_id": correlation_id,
    }


async def resolve_app_authorization(authorization: str) -> AppPrincipal | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    claims = decode_app_token(authorization[7:])
    if claims is None:
        return None
    try:
        app_id = uuid.UUID(str(claims["sub"]))
        credential_id = uuid.UUID(str(claims["cid"]))
        generation = int(claims["gen"])
        expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT deployment
              FROM app_credentials
             WHERE id = $1
               AND app_id = $2
               AND generation = $3
               AND status = 'active'
               AND (expires_at IS NULL OR expires_at > NOW())
            """,
            credential_id,
            app_id,
            generation,
        )
    if row is None:
        return None
    return AppPrincipal(
        app_id=app_id,
        credential_id=credential_id,
        credential_generation=generation,
        deployment=row["deployment"],
        token_id=str(claims["jti"]),
        expires_at=expires_at,
    )


async def authorize_app_request(
    principal: AppPrincipal,
    *,
    vault_id: uuid.UUID,
    capability: str,
    correlation_id: str,
    resource_kind: str | None = None,
    resource_key: str | None = None,
    conn=None,
) -> None:
    """Default-deny a control-plane operation against the live registry.

    Callers that perform a state change should pass their transaction
    connection and execute the operation in that same transaction.
    """
    if capability not in SUPPORTED_APP_CAPABILITIES:
        record_app_audit(
            "app.capability.denied",
            correlation_id=correlation_id,
            outcome="error",
            reason="unsupported_capability",
            actor=f"app:{principal.app_id}",
            actor_id=str(principal.app_id),
            app_id=principal.app_id,
            deployment=principal.deployment,
            vault_id=vault_id,
            generation=principal.credential_generation,
        )
        raise ForbiddenError("App request denied")
    if (resource_kind is None) != (resource_key is None):
        raise ValidationError("resource_kind and resource_key must be provided together")

    async def _check(active_conn):
        return await active_conn.fetchrow(
            """
            SELECT installation.id AS installation_id,
                   EXISTS (
                       SELECT 1
                         FROM installation_grants AS grant_row
                        WHERE grant_row.installation_id = installation.id
                          AND grant_row.generation = installation.grant_generation
                          AND grant_row.status = 'active'
                          AND $3 = ANY(grant_row.capabilities)
                   ) AS grant_allowed,
                   CASE
                       WHEN $4::text IS NULL THEN TRUE
                       ELSE EXISTS (
                           SELECT 1
                             FROM app_owned_resources AS resource
                            WHERE resource.installation_id = installation.id
                              AND resource.vault_id = installation.vault_id
                              AND resource.resource_kind = $4
                              AND resource.resource_key = $5
                              AND resource.status = 'owned'
                       )
                   END AS resource_allowed
              FROM vault_app_installations AS installation
             WHERE installation.app_id = $1
               AND installation.vault_id = $2
               AND installation.lifecycle = 'active'
            """,
            principal.app_id,
            vault_id,
            capability,
            resource_kind,
            resource_key,
        )

    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as acquired:
            row = await _check(acquired)
    else:
        row = await _check(conn)

    if row is None or not row["grant_allowed"] or not row["resource_allowed"]:
        reason = (
            "inactive_or_foreign_installation"
            if row is None
            else "capability_not_granted"
            if not row["grant_allowed"]
            else "resource_not_owned"
        )
        record_app_audit(
            "app.capability.denied",
            correlation_id=correlation_id,
            outcome="error",
            reason=reason,
            actor=f"app:{principal.app_id}",
            actor_id=str(principal.app_id),
            app_id=principal.app_id,
            deployment=principal.deployment,
            installation_id=row["installation_id"] if row else None,
            vault_id=vault_id,
            generation=principal.credential_generation,
        )
        raise ForbiddenError("App request denied")

    record_app_audit(
        "app.capability.authorized",
        correlation_id=correlation_id,
        outcome="ok",
        reason="authorized",
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
        app_id=principal.app_id,
        deployment=principal.deployment,
        installation_id=row["installation_id"],
        vault_id=vault_id,
        generation=principal.credential_generation,
    )


async def authorize_app_capability(
    principal: AppPrincipal,
    *,
    capability: str,
    correlation_id: str,
    conn=None,
) -> None:
    """Authorize an app-scoped control-plane capability.

    Inventory and rollout reads address an app as a whole rather than one
    Vault, so the per-installation ``authorize_app_request`` helper cannot be
    used as their boundary.  This check still binds authority to the live
    registry: at least one active installation for the token's app must have
    the current active grant with the requested capability.
    """
    if capability not in SUPPORTED_APP_CAPABILITIES:
        record_app_audit(
            "app.capability.denied",
            correlation_id=correlation_id,
            outcome="error",
            reason="unsupported_capability",
            actor=f"app:{principal.app_id}",
            actor_id=str(principal.app_id),
            app_id=principal.app_id,
            deployment=principal.deployment,
            generation=principal.credential_generation,
        )
        raise ForbiddenError("App request denied")

    async def _check(active_conn):
        return await active_conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM vault_app_installations AS installation
                  JOIN installation_grants AS grant_row
                    ON grant_row.installation_id = installation.id
                   AND grant_row.generation = installation.grant_generation
                   AND grant_row.status = 'active'
                 WHERE installation.app_id = $1
                   AND installation.lifecycle = 'active'
                   AND $2 = ANY(grant_row.capabilities)
            )
            """,
            principal.app_id,
            capability,
        )

    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as acquired:
            allowed = await _check(acquired)
    else:
        allowed = await _check(conn)

    if not allowed:
        record_app_audit(
            "app.capability.denied",
            correlation_id=correlation_id,
            outcome="error",
            reason="capability_not_granted",
            actor=f"app:{principal.app_id}",
            actor_id=str(principal.app_id),
            app_id=principal.app_id,
            deployment=principal.deployment,
            generation=principal.credential_generation,
        )
        raise ForbiddenError("App request denied")

    record_app_audit(
        "app.capability.authorized",
        correlation_id=correlation_id,
        outcome="ok",
        reason="authorized",
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
        app_id=principal.app_id,
        deployment=principal.deployment,
        generation=principal.credential_generation,
    )
