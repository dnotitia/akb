"""App installation lifecycle commands and status projection.

The desired-state registry is the source of truth for installation lifecycle.
Commands use the registry's existing uniqueness, generation, and immutability
constraints; no separate command ledger is needed.  An app/Vault advisory
transaction lock makes a retry or conflicting concurrent command observe one
serialized state transition.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Sequence

from app.db.postgres import get_pool
from app.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.services.access_service import check_vault_access
from app.services.app_identity_service import (
    AppPrincipal,
    SUPPORTED_APP_CAPABILITIES,
    record_app_audit,
)
from app.services.app_inventory_service import (
    classify_drift,
    expected_schema_fingerprint,
    sanitize_checkpoint,
    sanitize_recent_error,
)
from app.services.auth_service import AuthenticatedUser

LIFECYCLE_MODES = frozenset({"install", "restore", "fresh"})
READABLE_APP_LIFECYCLES = frozenset({"installing", "active", "upgrading", "blocked"})

_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SAFE_FINGERPRINT = re.compile(r"^[0-9A-Fa-f]{8,256}$")
_APP_VAULT_LOCK_PREFIX = "app-installation"


def _as_uuid(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"{field} must be a UUID") from exc


def normalize_mode(mode: str | None) -> str:
    value = "install" if mode is None else mode
    if value not in LIFECYCLE_MODES:
        raise ValidationError("mode must be one of install, restore, or fresh")
    return value


def normalize_capabilities(capabilities: Sequence[str]) -> list[str]:
    if isinstance(capabilities, (str, bytes)) or not isinstance(capabilities, Sequence):
        raise ValidationError("capabilities must be a non-empty list")
    if not capabilities:
        raise ValidationError("capabilities must not be empty")
    if any(not isinstance(capability, str) or not capability for capability in capabilities):
        raise ValidationError("capabilities must contain non-empty strings")
    normalized = sorted(set(capabilities))
    if not set(normalized).issubset(SUPPORTED_APP_CAPABILITIES):
        raise ValidationError("capabilities contain an unsupported value")
    return normalized


def _safe_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_CODE.fullmatch(value) else None


def _safe_fingerprint(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_FINGERPRINT.fullmatch(value) else None


def _release_payload(release_id: Any, version: Any) -> dict[str, Any] | None:
    if release_id is None and version is None:
        return None
    return {
        "id": str(release_id) if release_id is not None else None,
        "version": version,
    }


def _capabilities(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({item for item in value if isinstance(item, str)})


def _resource_payload(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        key = item.get("key")
        status = item.get("status")
        if not all(isinstance(field, str) for field in (kind, key, status)):
            continue
        result.append(
            {
                "kind": str(kind),
                "key": str(key),
                "status": str(status),
            }
        )
    return result


def project_installation_status(row: Any) -> dict[str, Any]:
    """Project the public lifecycle status without secret/provenance fields."""
    latest_grant = None
    if row["latest_grant_generation"] is not None:
        latest_grant = {
            "generation": row["latest_grant_generation"],
            "status": row["latest_grant_status"],
            "capabilities": _capabilities(row["latest_grant_capabilities"]),
        }

    latest_active_grant = None
    if row["latest_active_grant_generation"] is not None:
        latest_active_grant = {
            "generation": row["latest_active_grant_generation"],
            "status": row["latest_active_grant_status"],
            "capabilities": _capabilities(row["latest_active_grant_capabilities"]),
        }

    observed = None
    if row["observed_generation"] is not None:
        observed = {
            "generation": row["observed_generation"],
            "observed_at": (
                row["observed_at"].isoformat() if row["observed_at"] else None
            ),
            "release": _release_payload(
                row["observed_release_id"],
                row["observed_release_version"],
            ),
            "schema_fingerprint": _safe_fingerprint(row["schema_fingerprint"]),
            "grant_generation": row["observed_grant_generation"],
            "checkpoint": sanitize_checkpoint(row["checkpoint"]),
            "recent_error": sanitize_recent_error(row["recent_error"]),
        }

    drift = classify_drift(row)
    result = {
        "installation_id": str(row["installation_id"]),
        "app_id": str(row["app_id"]),
        "vault_id": str(row["vault_id"]),
        "lifecycle": row["lifecycle"],
        "desired_release": _release_payload(
            row["desired_release_id"],
            row["desired_version"],
        ),
        "current_release": _release_payload(
            row["current_release_id"],
            row["current_version"],
        ),
        "observed": observed,
        "latest_grant": latest_grant,
        "latest_active_grant": latest_active_grant,
        "grant_generation": row["grant_generation"],
        "resources": _resource_payload(row["resources"]),
        "checkpoint": (
            sanitize_checkpoint(row["checkpoint"])
            if row["observed_generation"] is not None
            else {}
        ),
        "recent_error": (
            sanitize_recent_error(row["recent_error"])
            if row["observed_generation"] is not None
            else None
        ),
        "drift": drift,
        "drift_classification": drift,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }
    blocked_reason = _safe_code(row["blocked_reason"])
    if blocked_reason is not None:
        result["blocked_reason"] = blocked_reason
    return result


_STATUS_SELECT = """
    SELECT
        installation.id AS installation_id,
        installation.app_id,
        installation.vault_id,
        installation.lifecycle,
        installation.blocked_reason,
        installation.desired_release_id,
        desired.version AS desired_version,
        desired.manifest AS desired_manifest,
        installation.current_release_id,
        current_release.version AS current_version,
        installation.grant_generation,
        latest_grant.generation AS latest_grant_generation,
        latest_grant.status AS latest_grant_status,
        latest_grant.capabilities AS latest_grant_capabilities,
        latest_active_grant.generation AS latest_active_grant_generation,
        latest_active_grant.status AS latest_active_grant_status,
        latest_active_grant.capabilities AS latest_active_grant_capabilities,
        observed.observed_generation,
        observed.observed_at,
        observed.observed_release_id,
        observed.observed_release_version,
        observed.schema_fingerprint,
        observed.observed_grant_generation,
        observed.checkpoint,
        observed.recent_error,
        COALESCE(resources.items, '[]'::jsonb) AS resources,
        installation.created_at,
        installation.updated_at
      FROM vault_app_installations AS installation
      LEFT JOIN app_releases AS desired
        ON desired.id = installation.desired_release_id
      LEFT JOIN app_releases AS current_release
        ON current_release.id = installation.current_release_id
      LEFT JOIN app_installation_observed_states AS observed
        ON observed.installation_id = installation.id
      LEFT JOIN LATERAL (
          SELECT grant_row.generation, grant_row.status, grant_row.capabilities
            FROM installation_grants AS grant_row
           WHERE grant_row.installation_id = installation.id
           ORDER BY grant_row.generation DESC
           LIMIT 1
      ) AS latest_grant ON TRUE
      LEFT JOIN LATERAL (
          SELECT grant_row.generation, grant_row.status, grant_row.capabilities
            FROM installation_grants AS grant_row
           WHERE grant_row.installation_id = installation.id
             AND grant_row.status = 'active'
           ORDER BY grant_row.generation DESC
           LIMIT 1
      ) AS latest_active_grant ON TRUE
      LEFT JOIN LATERAL (
          SELECT jsonb_agg(
                     jsonb_build_object(
                         'kind', resource.resource_kind,
                         'key', resource.resource_key,
                         'status', resource.status
                     )
                     ORDER BY resource.resource_kind, resource.resource_key
                 ) AS items
            FROM app_owned_resources AS resource
           WHERE resource.installation_id = installation.id
      ) AS resources ON TRUE
"""


async def authorize_lifecycle_admin(
    user: AuthenticatedUser,
    *,
    app_id: uuid.UUID | str,
    vault_id: uuid.UUID | str,
    action: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Authorize owner/admin access without an existence oracle.

    A non-admin caller gets the same generic 403 when the Vault is missing or
    the caller lacks the required role.  Authorized system admins can receive
    the normal not-found response for a missing Vault.
    """
    app_uuid = _as_uuid(app_id, field="app_id")
    vault_uuid = _as_uuid(vault_id, field="vault_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault_name = await conn.fetchval(
            "SELECT name FROM vaults WHERE id = $1",
            vault_uuid,
        )

    if vault_name is None:
        if user.is_admin:
            record_app_audit(
                action,
                correlation_id=correlation_id,
                outcome="error",
                reason="vault_not_found",
                actor=user.username,
                actor_id=user.user_id,
                app_id=app_uuid,
                vault_id=vault_uuid,
            )
            raise NotFoundError("Vault", str(vault_uuid))
        _record_user_denial(
            action,
            correlation_id=correlation_id,
            app_id=app_uuid,
            vault_id=vault_uuid,
            user=user,
            reason="vault_admin_required",
        )

    try:
        access = await check_vault_access(
            user.user_id,
            vault_name,
            required_role="admin",
        )
    except (ForbiddenError, NotFoundError):
        _record_user_denial(
            action,
            correlation_id=correlation_id,
            app_id=app_uuid,
            vault_id=vault_uuid,
            user=user,
            reason="vault_admin_required",
        )
    return access


def _record_user_denial(
    action: str,
    *,
    correlation_id: str,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    user: AuthenticatedUser,
    reason: str,
) -> None:
    record_app_audit(
        action,
        correlation_id=correlation_id,
        outcome="error",
        reason=reason,
        actor=user.username,
        actor_id=user.user_id,
        app_id=app_id,
        vault_id=vault_id,
    )
    raise ForbiddenError("App request denied")


def _record_command_audit(
    action: str,
    *,
    correlation_id: str,
    actor: str,
    actor_id: str,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    status: dict[str, Any],
    replayed: bool,
    deployment: str | None = None,
) -> None:
    record_app_audit(
        action,
        correlation_id=correlation_id,
        outcome="ok",
        reason="already_applied" if replayed else "accepted",
        actor=actor,
        actor_id=actor_id,
        app_id=app_id,
        deployment=deployment,
        installation_id=status["installation_id"],
        vault_id=vault_id,
        generation=status["grant_generation"],
    )


def _record_command_error(
    action: str,
    *,
    correlation_id: str,
    actor: str,
    actor_id: str,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    exc: Exception,
    deployment: str | None = None,
) -> None:
    if isinstance(exc, ValidationError):
        reason = "invalid_request"
    elif isinstance(exc, ConflictError):
        reason = "conflict"
    elif isinstance(exc, NotFoundError):
        reason = "not_found"
    elif isinstance(exc, ForbiddenError):
        reason = "denied"
    else:
        reason = "error"
    record_app_audit(
        action,
        correlation_id=correlation_id,
        outcome="error",
        reason=reason,
        actor=actor,
        actor_id=actor_id,
        app_id=app_id,
        deployment=deployment,
        vault_id=vault_id,
    )


async def _status_row(conn, app_id: uuid.UUID, vault_id: uuid.UUID) -> Any:
    return await conn.fetchrow(
        _STATUS_SELECT
        + """
         WHERE installation.app_id = $1
           AND installation.vault_id = $2
        """,
        app_id,
        vault_id,
    )


async def get_installation_status(
    app_id: uuid.UUID | str,
    vault_id: uuid.UUID | str,
) -> dict[str, Any]:
    app_uuid = _as_uuid(app_id, field="app_id")
    vault_uuid = _as_uuid(vault_id, field="vault_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _status_row(conn, app_uuid, vault_uuid)
    if row is None:
        raise NotFoundError("Installation", "not found")
    result = project_installation_status(row)
    result["command_status"] = "not_applicable"
    return result


async def get_installation_status_for_app(
    principal: AppPrincipal,
    *,
    vault_id: uuid.UUID | str,
    correlation_id: str,
) -> dict[str, Any]:
    vault_uuid = _as_uuid(vault_id, field="vault_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _STATUS_SELECT
            + """
             WHERE installation.app_id = $1
               AND installation.vault_id = $2
               AND installation.lifecycle = ANY($3::text[])
               AND EXISTS (
                   SELECT 1
                     FROM installation_grants AS visible_grant
                    WHERE visible_grant.installation_id = installation.id
                      AND visible_grant.generation = installation.grant_generation
                      AND visible_grant.status = 'active'
                      AND 'installation:read' = ANY(visible_grant.capabilities)
               )
            """,
            principal.app_id,
            vault_uuid,
            list(READABLE_APP_LIFECYCLES),
        )
    if row is None:
        record_app_audit(
            "app.installation.status",
            correlation_id=correlation_id,
            outcome="error",
            reason="installation_read_denied",
            actor=f"app:{principal.app_id}",
            actor_id=str(principal.app_id),
            app_id=principal.app_id,
            deployment=principal.deployment,
            vault_id=vault_uuid,
            generation=principal.credential_generation,
        )
        raise ForbiddenError("App request denied")
    result = project_installation_status(row)
    result["command_status"] = "not_applicable"
    record_app_audit(
        "app.installation.status",
        correlation_id=correlation_id,
        outcome="ok",
        reason="authorized",
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
        app_id=principal.app_id,
        deployment=principal.deployment,
        installation_id=row["installation_id"],
        vault_id=vault_uuid,
        generation=row["grant_generation"],
    )
    return result


async def put_installation(
    app_id: uuid.UUID | str,
    vault_id: uuid.UUID | str,
    *,
    release_id: uuid.UUID | str,
    capabilities: Sequence[str],
    mode: str | None,
    correlation_id: str,
    actor: str,
    actor_id: str,
) -> dict[str, Any]:
    app_uuid = _as_uuid(app_id, field="app_id")
    vault_uuid = _as_uuid(vault_id, field="vault_id")
    action = (
        f"app.installation.{mode}"
        if isinstance(mode, str) and mode in LIFECYCLE_MODES
        else "app.installation.command"
    )
    try:
        release_uuid = _as_uuid(release_id, field="release_id")
        normalized_mode = normalize_mode(mode)
        normalized_capabilities = normalize_capabilities(capabilities)
    except Exception as exc:
        _record_command_error(
            action,
            correlation_id=correlation_id,
            actor=actor,
            actor_id=actor_id,
            app_id=app_uuid,
            vault_id=vault_uuid,
            exc=exc,
        )
        raise
    action = f"app.installation.{normalized_mode}"
    try:
        result, replayed = await _put_installation(
            app_uuid,
            vault_uuid,
            release_uuid,
            normalized_capabilities,
            normalized_mode,
        )
    except Exception as exc:
        _record_command_error(
            action,
            correlation_id=correlation_id,
            actor=actor,
            actor_id=actor_id,
            app_id=app_uuid,
            vault_id=vault_uuid,
            exc=exc,
        )
        raise
    _record_command_audit(
        action,
        correlation_id=correlation_id,
        actor=actor,
        actor_id=actor_id,
        app_id=app_uuid,
        vault_id=vault_uuid,
        status=result,
        replayed=replayed,
    )
    result["command_status"] = "already_applied" if replayed else "accepted"
    result["replayed"] = replayed
    return result


async def _put_installation(
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    release_id: uuid.UUID,
    capabilities: list[str],
    mode: str,
) -> tuple[dict[str, Any], bool]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"{_APP_VAULT_LOCK_PREFIX}:{app_id}:{vault_id}",
            )
            if not await conn.fetchval(
                "SELECT 1 FROM app_definitions WHERE id = $1",
                app_id,
            ):
                raise NotFoundError("App", "not found")
            if not await conn.fetchval("SELECT 1 FROM vaults WHERE id = $1", vault_id):
                raise NotFoundError("Vault", "not found")
            if not await conn.fetchval(
                "SELECT 1 FROM app_releases WHERE id = $1 AND app_id = $2",
                release_id,
                app_id,
            ):
                raise ConflictError("Release is not registered for this app")

            installation = await conn.fetchrow(
                """
                SELECT *
                  FROM vault_app_installations
                 WHERE app_id = $1 AND vault_id = $2
                 FOR UPDATE
                """,
                app_id,
                vault_id,
            )
            if installation is None:
                if mode != "install":
                    raise ConflictError("restore or fresh requires an uninstalled installation")
                installation_id = await conn.fetchval(
                    """
                    INSERT INTO vault_app_installations (
                        app_id, vault_id, desired_release_id, current_release_id,
                        lifecycle, blocked_reason
                    )
                    VALUES ($1, $2, $3, NULL, 'installing', NULL)
                    RETURNING id
                    """,
                    app_id,
                    vault_id,
                    release_id,
                )
                await _issue_grant(conn, installation_id, 1, capabilities)
                row = await _status_row(conn, app_id, vault_id)
                return _require_status(row), False

            current_grant = await conn.fetchrow(
                """
                SELECT generation, status, capabilities
                  FROM installation_grants
                 WHERE installation_id = $1
                   AND generation = $2
                """,
                installation["id"],
                installation["grant_generation"],
            )
            if _is_replay_state(installation, current_grant, release_id, capabilities):
                row = await _status_row(conn, app_id, vault_id)
                return _require_status(row), True

            if mode == "install":
                raise ConflictError(
                    "Installation state differs; use restore or fresh explicitly"
                )
            if installation["lifecycle"] != "uninstalled":
                raise ConflictError("restore or fresh requires an uninstalled installation")

            if mode == "restore":
                await _assert_restore_compatible(
                    conn,
                    installation,
                    release_id,
                )
                await conn.execute(
                    """
                    UPDATE app_owned_resources
                       SET status = 'owned'
                     WHERE installation_id = $1 AND status = 'retained'
                    """,
                    installation["id"],
                )
                await conn.execute(
                    """
                    UPDATE vault_app_installations
                       SET desired_release_id = $2,
                           current_release_id = $2,
                           lifecycle = 'active',
                           blocked_reason = NULL
                     WHERE id = $1
                    """,
                    installation["id"],
                    release_id,
                )
            else:
                if await conn.fetchval(
                    """
                    SELECT 1 FROM app_owned_resources
                     WHERE installation_id = $1
                    """,
                    installation["id"],
                ):
                    raise ConflictError("Fresh install is blocked by retained resources")
                await conn.execute(
                    """
                    UPDATE vault_app_installations
                       SET desired_release_id = $2,
                           current_release_id = NULL,
                           lifecycle = 'installing',
                           blocked_reason = NULL
                     WHERE id = $1
                    """,
                    installation["id"],
                    release_id,
                )

            await _issue_grant(
                conn,
                installation["id"],
                installation["grant_generation"] + 1,
                capabilities,
            )
            row = await _status_row(conn, app_id, vault_id)
            return _require_status(row), False


def _require_status(row: Any) -> Any:
    if row is None:
        raise ConflictError("Installation status could not be read")
    return project_installation_status(row)


def _is_replay_state(
    installation: Any,
    grant: Any,
    release_id: uuid.UUID,
    capabilities: list[str],
) -> bool:
    if installation["lifecycle"] not in {"installing", "active"}:
        return False
    if installation["desired_release_id"] != release_id:
        return False
    if installation["lifecycle"] == "installing":
        if installation["current_release_id"] is not None:
            return False
    elif installation["current_release_id"] != release_id:
        return False
    return bool(
        grant
        and grant["status"] == "active"
        and grant["generation"] == installation["grant_generation"]
        and _capabilities(grant["capabilities"]) == capabilities
    )


async def _issue_grant(
    conn: Any,
    installation_id: uuid.UUID,
    generation: int,
    capabilities: list[str],
) -> None:
    await conn.execute(
        """
        INSERT INTO installation_grants (
            installation_id, generation, status, capabilities, issuer, provenance
        )
        VALUES ($1, $2, 'active', $3, 'lifecycle_api', $4::jsonb)
        """,
        installation_id,
        generation,
        capabilities,
        json.dumps({"source": "lifecycle_api"}),
    )


async def _assert_restore_compatible(
    conn: Any,
    installation: Any,
    release_id: uuid.UUID,
) -> None:
    if installation["current_release_id"] != release_id:
        raise ConflictError("Restore release is not compatible with the retained installation")
    release = await conn.fetchrow(
        "SELECT manifest FROM app_releases WHERE id = $1 AND app_id = $2",
        release_id,
        installation["app_id"],
    )
    observed = await conn.fetchrow(
        """
        SELECT observed_release_id, schema_fingerprint
          FROM app_installation_observed_states
         WHERE installation_id = $1
        """,
        installation["id"],
    )
    expected_schema = expected_schema_fingerprint(release["manifest"]) if release else None
    observed_schema = observed["schema_fingerprint"] if observed else None
    if (
        release is None
        or observed is None
        or observed["observed_release_id"] != release_id
        or expected_schema is None
        or observed_schema is None
        or expected_schema != observed_schema
    ):
        raise ConflictError("Restore compatibility is unknown or mismatched")


async def uninstall_installation(
    app_id: uuid.UUID | str,
    vault_id: uuid.UUID | str,
    *,
    correlation_id: str,
    actor: str,
    actor_id: str,
) -> dict[str, Any]:
    app_uuid = _as_uuid(app_id, field="app_id")
    vault_uuid = _as_uuid(vault_id, field="vault_id")
    action = "app.installation.uninstall"
    try:
        result, replayed = await _uninstall_installation(app_uuid, vault_uuid)
    except Exception as exc:
        _record_command_error(
            action,
            correlation_id=correlation_id,
            actor=actor,
            actor_id=actor_id,
            app_id=app_uuid,
            vault_id=vault_uuid,
            exc=exc,
        )
        raise
    _record_command_audit(
        action,
        correlation_id=correlation_id,
        actor=actor,
        actor_id=actor_id,
        app_id=app_uuid,
        vault_id=vault_uuid,
        status=result,
        replayed=replayed,
    )
    result["command_status"] = "already_applied" if replayed else "accepted"
    result["replayed"] = replayed
    return result


async def _uninstall_installation(
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
) -> tuple[dict[str, Any], bool]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"{_APP_VAULT_LOCK_PREFIX}:{app_id}:{vault_id}",
            )
            if not await conn.fetchval(
                "SELECT 1 FROM app_definitions WHERE id = $1",
                app_id,
            ):
                raise NotFoundError("App", "not found")
            installation = await conn.fetchrow(
                """
                SELECT *
                  FROM vault_app_installations
                 WHERE app_id = $1 AND vault_id = $2
                 FOR UPDATE
                """,
                app_id,
                vault_id,
            )
            if installation is None:
                raise NotFoundError("Installation", "not found")
            if installation["lifecycle"] == "uninstalled":
                row = await _status_row(conn, app_id, vault_id)
                return _require_status(row), True

            grant = await conn.fetchrow(
                """
                SELECT id, generation, status
                  FROM installation_grants
                 WHERE installation_id = $1
                   AND generation = $2
                """,
                installation["id"],
                installation["grant_generation"],
            )
            if grant is None or grant["status"] != "active":
                raise ConflictError("Installation has no active current grant")
            await conn.execute(
                """
                UPDATE installation_grants
                   SET status = 'revoked', revoked_at = NOW()
                 WHERE id = $1 AND status = 'active'
                """,
                grant["id"],
            )
            await conn.execute(
                """
                UPDATE app_owned_resources
                   SET status = 'retained'
                 WHERE installation_id = $1 AND status = 'owned'
                """,
                installation["id"],
            )
            await conn.execute(
                """
                UPDATE vault_app_installations
                   SET desired_release_id = NULL,
                       lifecycle = 'uninstalled',
                       blocked_reason = NULL
                 WHERE id = $1
                """,
                installation["id"],
            )
            row = await _status_row(conn, app_id, vault_id)
            return _require_status(row), False
