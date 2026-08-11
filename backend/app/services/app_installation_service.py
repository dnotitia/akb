"""App installation lifecycle commands and status projection.

The app registry is the source of truth for lifecycle state.  Commands use a
transaction-scoped advisory lock for one app/Vault pair, then rely on the
existing registry constraints and grant-generation trigger for atomicity.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import (
    AKBError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.services.access_service import check_vault_access
from app.services.app_identity_service import (
    SUPPORTED_APP_CAPABILITIES,
    AppPrincipal,
    record_app_audit,
)
from app.services.app_inventory_service import (
    classify_drift,
    sanitize_checkpoint,
    sanitize_recent_error,
)
from app.services.auth_service import AuthenticatedUser

LIFECYCLE_MODES = frozenset({"install", "restore", "fresh"})
READABLE_LIFECYCLES = frozenset({"installing", "active", "upgrading", "blocked"})
_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SAFE_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$")
_SAFE_FINGERPRINT = re.compile(r"^[0-9A-Fa-f]{8,256}$")


def normalize_capabilities(capabilities: list[str] | tuple[str, ...]) -> list[str]:
    """Validate the exact app capability allowlist and return sorted uniques."""

    if not isinstance(capabilities, (list, tuple)) or not capabilities:
        raise ValidationError("capabilities must not be empty")
    if any(not isinstance(value, str) for value in capabilities):
        raise ValidationError("capabilities must contain strings")
    normalized = sorted(set(capabilities))
    if not normalized or any(value not in SUPPORTED_APP_CAPABILITIES for value in normalized):
        raise ValidationError("capabilities contain an unsupported value")
    return normalized


def normalize_mode(mode: str | None) -> str:
    value = "install" if mode is None else mode
    if value not in LIFECYCLE_MODES:
        raise ValidationError("mode must be install, restore, or fresh")
    return value


def _as_uuid(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a UUID") from exc


def _safe_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_CODE.fullmatch(value) else None


def _safe_release(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_RELEASE.fullmatch(value) else None


def _safe_fingerprint(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_FINGERPRINT.fullmatch(value) else None


def _expected_schema_fingerprint(manifest: Any) -> str | None:
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(manifest, dict):
        return None
    candidates = (
        manifest.get("expected_schema_fingerprint"),
        manifest.get("schema_fingerprint"),
        manifest.get("schema", {}).get("fingerprint")
        if isinstance(manifest.get("schema"), dict)
        else None,
        manifest.get("schema", {}).get("expected_fingerprint")
        if isinstance(manifest.get("schema"), dict)
        else None,
    )
    for candidate in candidates:
        safe = _safe_fingerprint(candidate)
        if safe is not None:
            return safe
    return None


def _release_payload(release_id: Any, version: Any) -> dict[str, Any] | None:
    if release_id is None and version is None:
        return None
    return {
        "id": str(release_id) if release_id is not None else None,
        "version": _safe_release(version),
    }


def _grant_payload(row: Any, *, prefix: str = "") -> dict[str, Any] | None:
    generation = row[f"{prefix}grant_generation"]
    if generation is None:
        return None
    capabilities = row[f"{prefix}grant_capabilities"] or []
    return {
        "generation": generation,
        "status": row[f"{prefix}grant_status"],
        "capabilities": sorted(
            value for value in capabilities if isinstance(value, str)
        ),
    }


def _observed_payload(row: Any) -> dict[str, Any] | None:
    if row["observed_generation"] is None:
        return None
    observed_at = row["observed_at"]
    return {
        "generation": row["observed_generation"],
        "observed_at": observed_at.isoformat() if observed_at else None,
        "release": _release_payload(
            row["observed_release_id"],
            row["observed_release_version"],
        ),
        "schema_fingerprint": _safe_fingerprint(row["schema_fingerprint"]),
        "grant_generation": row["observed_grant_generation"],
        "checkpoint": sanitize_checkpoint(row["checkpoint"]),
        "recent_error": sanitize_recent_error(row["recent_error"]),
    }


def project_installation(row: Any, resources: list[Any]) -> dict[str, Any]:
    """Return the bounded public status projection for one installation."""

    row_dict = dict(row)
    row_dict["grant_generation"] = row["desired_grant_generation"]
    drift = classify_drift(row_dict)
    latest_grant = _grant_payload(row)
    active_grant = _grant_payload(row, prefix="active_")
    return {
        "installation_id": str(row["installation_id"]),
        "app_id": str(row["app_id"]),
        "vault_id": str(row["vault_id"]),
        "lifecycle": row["lifecycle"],
        "blocked_reason": _safe_code(row["blocked_reason"]),
        "desired_release": _release_payload(
            row["desired_release_id"],
            row["desired_version"],
        ),
        "current_release": _release_payload(
            row["current_release_id"],
            row["current_version"],
        ),
        "observed": _observed_payload(row),
        "desired_grant_generation": row["desired_grant_generation"],
        "latest_grant": latest_grant,
        "active_grant": active_grant,
        "owned_resources": [
            {
                "kind": resource["resource_kind"],
                "key": resource["resource_key"],
                "status": resource["status"],
            }
            for resource in resources
        ],
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
        "command_status": "not_applicable",
    }


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
        installation.grant_generation AS desired_grant_generation,
        latest_grant.generation AS grant_generation,
        latest_grant.status AS grant_status,
        latest_grant.capabilities AS grant_capabilities,
        active_grant.generation AS active_grant_generation,
        active_grant.status AS active_grant_status,
        active_grant.capabilities AS active_grant_capabilities,
        observed.observed_generation,
        observed.observed_at,
        observed.observed_release_id,
        observed.observed_release_version,
        observed.schema_fingerprint,
        observed.observed_grant_generation,
        observed.checkpoint,
        observed.recent_error,
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
      ) AS active_grant ON TRUE
"""


async def _fetch_projection(
    conn: Any,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        _STATUS_SELECT + " WHERE installation.app_id = $1 AND installation.vault_id = $2",
        app_id,
        vault_id,
    )
    if row is None:
        return None
    resources = await conn.fetch(
        """
        SELECT resource_kind, resource_key, status
          FROM app_owned_resources
         WHERE installation_id = $1
         ORDER BY resource_kind, resource_key
        """,
        row["installation_id"],
    )
    return project_installation(row, resources)


async def _authorize_vault_admin(
    user: AuthenticatedUser,
    vault_id: uuid.UUID,
    *,
    app_id: uuid.UUID,
    action: str,
    correlation_id: str,
) -> str:
    """Authorize before resolving app/release/installation metadata."""

    pool = await get_pool()
    async with pool.acquire() as conn:
        vault_name = await conn.fetchval(
            "SELECT name FROM vaults WHERE id = $1",
            vault_id,
        )
    if vault_name is None:
        if user.is_admin:
            raise NotFoundError("Vault", "not found")
        record_app_audit(
            action,
            correlation_id=correlation_id,
            outcome="error",
            reason="vault_admin_required",
            actor=user.username,
            actor_id=user.user_id,
            app_id=app_id,
            vault_id=vault_id,
        )
        raise ForbiddenError("Installation request denied")

    try:
        await check_vault_access(user.user_id, vault_name, required_role="admin")
    except NotFoundError:
        if user.is_admin:
            raise
        record_app_audit(
            action,
            correlation_id=correlation_id,
            outcome="error",
            reason="vault_admin_required",
            actor=user.username,
            actor_id=user.user_id,
            app_id=app_id,
            vault_id=vault_id,
        )
        raise ForbiddenError("Installation request denied") from None
    except ForbiddenError:
        record_app_audit(
            action,
            correlation_id=correlation_id,
            outcome="error",
            reason="vault_admin_required",
            actor=user.username,
            actor_id=user.user_id,
            app_id=app_id,
            vault_id=vault_id,
        )
        raise ForbiddenError("Installation request denied") from None
    return "system_admin" if user.is_admin else "vault_admin"


async def _authorize_command(
    user: AuthenticatedUser,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    *,
    action: str,
    correlation_id: str,
) -> str:
    return await _authorize_vault_admin(
        user,
        vault_id,
        app_id=app_id,
        action=action,
        correlation_id=correlation_id,
    )


async def _lock_pair(conn: Any, app_id: uuid.UUID, vault_id: uuid.UUID) -> None:
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"app-installation:{app_id}:{vault_id}",
    )


async def _load_context(
    conn: Any,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    release_id: uuid.UUID | None,
) -> tuple[Any, Any, Any | None]:
    app = await conn.fetchrow("SELECT id FROM app_definitions WHERE id = $1", app_id)
    if app is None:
        raise NotFoundError("App", "not found")
    vault = await conn.fetchrow("SELECT id FROM vaults WHERE id = $1", vault_id)
    if vault is None:
        raise NotFoundError("Vault", "not found")
    release = None
    if release_id is not None:
        release = await conn.fetchrow(
            "SELECT id, app_id, version, manifest FROM app_releases WHERE id = $1",
            release_id,
        )
        if release is None:
            raise NotFoundError("Release", "not found")
        if release["app_id"] != app_id:
            raise ConflictError("Release does not belong to app")
    return app, vault, release


async def _load_installation(conn: Any, app_id: uuid.UUID, vault_id: uuid.UUID) -> Any:
    return await conn.fetchrow(
        """
        SELECT *
          FROM vault_app_installations
         WHERE app_id = $1 AND vault_id = $2
         FOR UPDATE
        """,
        app_id,
        vault_id,
    )


async def _active_grant(conn: Any, installation_id: uuid.UUID) -> Any:
    return await conn.fetchrow(
        """
        SELECT generation, capabilities, provenance
          FROM installation_grants
         WHERE installation_id = $1 AND status = 'active'
         ORDER BY generation DESC
         LIMIT 1
        """,
        installation_id,
    )


def _grant_matches(grant: Any, capabilities: list[str], *, mode: str | None = None) -> bool:
    if grant is None or sorted(grant["capabilities"] or []) != capabilities:
        return False
    if mode is None:
        return True
    provenance = grant["provenance"]
    if isinstance(provenance, str):
        try:
            provenance = json.loads(provenance)
        except (TypeError, ValueError, json.JSONDecodeError):
            provenance = {}
    return isinstance(provenance, dict) and provenance.get("mode") == mode


def _command_payload(
    projection: dict[str, Any],
    *,
    command_status: str,
    replayed: bool,
) -> dict[str, Any]:
    result = dict(projection)
    result["command_status"] = command_status
    result["replayed"] = replayed
    return result


def _audit_reason(exc: AKBError) -> str:
    if isinstance(exc, ValidationError):
        return "invalid_request"
    if isinstance(exc, ConflictError):
        return "conflict"
    if isinstance(exc, NotFoundError):
        return "not_found"
    return "denied"


async def command_installation(
    app_id: uuid.UUID | str,
    vault_id: uuid.UUID | str,
    *,
    release_id: uuid.UUID | str,
    capabilities: list[str],
    mode: str | None,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    """Apply or replay an install/restore/fresh command."""

    app_id = _as_uuid(app_id, field="app_id")
    vault_id = _as_uuid(vault_id, field="vault_id")
    release_id = _as_uuid(release_id, field="release_id")
    normalized_capabilities = normalize_capabilities(capabilities)
    normalized_mode = normalize_mode(mode)
    action = f"app.installation.{normalized_mode}"
    actor_kind = await _authorize_command(
        user,
        app_id,
        vault_id,
        action=action,
        correlation_id=correlation_id,
    )

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _lock_pair(conn, app_id, vault_id)
                _app, _vault, release = await _load_context(
                    conn, app_id, vault_id, release_id
                )
                assert release is not None
                installation = await _load_installation(conn, app_id, vault_id)
                if installation is not None:
                    active_grant = await _active_grant(conn, installation["id"])
                    same_release = installation["desired_release_id"] == release_id
                    same_capabilities = _grant_matches(
                        active_grant,
                        normalized_capabilities,
                    )
                    replay = False
                    if normalized_mode == "install":
                        replay = same_release and same_capabilities and installation["lifecycle"] in {
                            "installing",
                            "active",
                        }
                    else:
                        replay = (
                            same_release
                            and same_capabilities
                            and _grant_matches(
                                active_grant,
                                normalized_capabilities,
                                mode=normalized_mode,
                            )
                            and (
                                (
                                    normalized_mode == "fresh"
                                    and installation["lifecycle"] == "installing"
                                    and installation["current_release_id"] is None
                                )
                                or (
                                    normalized_mode == "restore"
                                    and installation["lifecycle"] == "active"
                                    and installation["current_release_id"] == release_id
                                )
                            )
                        )
                    if replay:
                        projection = await _fetch_projection(conn, app_id, vault_id)
                        assert projection is not None
                        result = _command_payload(
                            projection,
                            command_status="already_applied",
                            replayed=True,
                        )
                    else:
                        result = await _apply_existing_command(
                            conn,
                            installation,
                            release,
                            release_id,
                            normalized_capabilities,
                            normalized_mode,
                            app_id,
                            vault_id,
                        )
                else:
                    if normalized_mode != "install":
                        raise ConflictError("Installation must exist before restore or fresh")
                    await conn.execute(
                        """
                        INSERT INTO vault_app_installations (
                            app_id, vault_id, desired_release_id, lifecycle
                        ) VALUES ($1, $2, $3, 'installing')
                        """,
                        app_id,
                        vault_id,
                        release_id,
                    )
                    installation = await _load_installation(conn, app_id, vault_id)
                    assert installation is not None
                    await _insert_grant(
                        conn,
                        installation["id"],
                        installation["grant_generation"] + 1,
                        normalized_capabilities,
                        normalized_mode,
                    )
                    projection = await _fetch_projection(conn, app_id, vault_id)
                    assert projection is not None
                    result = _command_payload(
                        projection,
                        command_status="accepted",
                        replayed=False,
                    )
    except asyncpg.UniqueViolationError:
        _record_command_error(
            action,
            actor_kind,
            user,
            app_id,
            vault_id,
            correlation_id,
            "conflict",
        )
        raise ConflictError("Installation command conflicted with another request") from None
    except (ValidationError, ConflictError, NotFoundError) as exc:
        _record_command_error(
            action,
            actor_kind,
            user,
            app_id,
            vault_id,
            correlation_id,
            _audit_reason(exc),
        )
        raise

    record_app_audit(
        action,
        correlation_id=correlation_id,
        outcome="ok",
        reason=result["command_status"],
        actor=actor_kind,
        actor_id=user.user_id,
        app_id=app_id,
        installation_id=result["installation_id"],
        vault_id=vault_id,
        generation=result["desired_grant_generation"],
    )
    return result


async def _apply_existing_command(
    conn: Any,
    installation: Any,
    release: Any,
    release_id: uuid.UUID,
    capabilities: list[str],
    mode: str,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
) -> dict[str, Any]:
    if mode == "install":
        if installation["lifecycle"] in {"blocked", "upgrading", "uninstalled"}:
            raise ConflictError("Installation state requires restore or fresh")
        raise ConflictError("Installation command conflicts with current state")

    if installation["lifecycle"] != "uninstalled":
        raise ConflictError("Restore or fresh requires an uninstalled installation")
    if mode == "restore" and installation["current_release_id"] != release_id:
        raise ConflictError("Restore command conflicts with retained state")

    if mode == "restore":
        observed = await conn.fetchrow(
            """
            SELECT observed_release_id, schema_fingerprint
              FROM app_installation_observed_states
             WHERE installation_id = $1
            """,
            installation["id"],
        )
        expected = _expected_schema_fingerprint(release["manifest"])
        if (
            observed is None
            or observed["observed_release_id"] != release_id
            or expected is None
            or _safe_fingerprint(observed["schema_fingerprint"]) != expected
        ):
            raise ConflictError("Restore compatibility check failed")
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
        retained_count = await conn.fetchval(
            """
            SELECT count(*)
              FROM app_owned_resources
             WHERE installation_id = $1 AND status = 'retained'
            """,
            installation["id"],
        )
        if retained_count:
            raise ConflictError("Fresh install conflicts with retained resources")
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

    await _insert_grant(
        conn,
        installation["id"],
        installation["grant_generation"] + 1,
        capabilities,
        mode,
    )
    projection = await _fetch_projection(conn, app_id, vault_id)
    assert projection is not None
    return _command_payload(projection, command_status="accepted", replayed=False)


async def _insert_grant(
    conn: Any,
    installation_id: uuid.UUID,
    generation: int,
    capabilities: list[str],
    mode: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO installation_grants (
            installation_id, generation, capabilities, issuer, provenance
        ) VALUES ($1, $2, $3, 'control-plane', $4::jsonb)
        """,
        installation_id,
        generation,
        capabilities,
        json.dumps({"source": "control_plane", "mode": mode}),
    )


def _record_command_error(
    action: str,
    actor_kind: str,
    user: AuthenticatedUser,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    correlation_id: str,
    reason: str,
) -> None:
    record_app_audit(
        action,
        correlation_id=correlation_id,
        outcome="error",
        reason=reason,
        actor=actor_kind,
        actor_id=user.user_id,
        app_id=app_id,
        vault_id=vault_id,
    )


async def uninstall_installation(
    app_id: uuid.UUID | str,
    vault_id: uuid.UUID | str,
    *,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    """Revoke the current grant and retain owned resources atomically."""

    app_id = _as_uuid(app_id, field="app_id")
    vault_id = _as_uuid(vault_id, field="vault_id")
    action = "app.installation.uninstall"
    actor_kind = await _authorize_command(
        user,
        app_id,
        vault_id,
        action=action,
        correlation_id=correlation_id,
    )
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _lock_pair(conn, app_id, vault_id)
                await _load_context(conn, app_id, vault_id, None)
                installation = await _load_installation(conn, app_id, vault_id)
                if installation is None:
                    raise NotFoundError("Installation", "not found")
                if installation["lifecycle"] == "uninstalled":
                    projection = await _fetch_projection(conn, app_id, vault_id)
                    assert projection is not None
                    result = _command_payload(
                        projection,
                        command_status="already_applied",
                        replayed=True,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE installation_grants
                           SET status = 'revoked', revoked_at = NOW()
                         WHERE installation_id = $1 AND status = 'active'
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
                    await conn.execute(
                        """
                        UPDATE app_owned_resources
                           SET status = 'retained'
                         WHERE installation_id = $1 AND status = 'owned'
                        """,
                        installation["id"],
                    )
                    projection = await _fetch_projection(conn, app_id, vault_id)
                    assert projection is not None
                    result = _command_payload(
                        projection,
                        command_status="accepted",
                        replayed=False,
                    )
    except (ValidationError, ConflictError, NotFoundError) as exc:
        _record_command_error(
            action,
            actor_kind,
            user,
            app_id,
            vault_id,
            correlation_id,
            _audit_reason(exc),
        )
        raise

    record_app_audit(
        action,
        correlation_id=correlation_id,
        outcome="ok",
        reason=result["command_status"],
        actor=actor_kind,
        actor_id=user.user_id,
        app_id=app_id,
        installation_id=result["installation_id"],
        vault_id=vault_id,
        generation=result["desired_grant_generation"],
    )
    return result


async def get_admin_installation_status(
    app_id: uuid.UUID | str,
    vault_id: uuid.UUID | str,
    *,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    app_id = _as_uuid(app_id, field="app_id")
    vault_id = _as_uuid(vault_id, field="vault_id")
    actor_kind = await _authorize_command(
        user,
        app_id,
        vault_id,
        action="app.installation.status",
        correlation_id=correlation_id,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _load_context(conn, app_id, vault_id, None)
        projection = await _fetch_projection(conn, app_id, vault_id)
    if projection is None:
        raise NotFoundError("Installation", "not found")
    record_app_audit(
        "app.installation.status",
        correlation_id=correlation_id,
        outcome="ok",
        reason="read",
        actor=actor_kind,
        actor_id=user.user_id,
        app_id=app_id,
        installation_id=projection["installation_id"],
        vault_id=vault_id,
        generation=projection["desired_grant_generation"],
    )
    return projection


async def get_app_installation_status(
    principal: AppPrincipal,
    vault_id: uuid.UUID | str,
    *,
    correlation_id: str,
) -> dict[str, Any]:
    """Read only the principal app's live, non-uninstalled installation."""

    vault_id = _as_uuid(vault_id, field="vault_id")
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
                     FROM installation_grants AS readable_grant
                    WHERE readable_grant.installation_id = installation.id
                      AND readable_grant.generation = installation.grant_generation
                      AND readable_grant.status = 'active'
                      AND 'installation:read' = ANY(readable_grant.capabilities)
               )
            """,
            principal.app_id,
            vault_id,
            list(READABLE_LIFECYCLES),
        )
        if row is None:
            record_app_audit(
                "app.installation.status",
                correlation_id=correlation_id,
                outcome="error",
                reason="app_request_denied",
                actor=f"app:{principal.app_id}",
                actor_id=str(principal.app_id),
                app_id=principal.app_id,
                vault_id=vault_id,
                generation=principal.credential_generation,
            )
            raise ForbiddenError("App request denied")
        resources = await conn.fetch(
            """
            SELECT resource_kind, resource_key, status
              FROM app_owned_resources
             WHERE installation_id = $1
             ORDER BY resource_kind, resource_key
            """,
            row["installation_id"],
        )
    result = project_installation(row, resources)
    record_app_audit(
        "app.installation.status",
        correlation_id=correlation_id,
        outcome="ok",
        reason="read",
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
        app_id=principal.app_id,
        installation_id=result["installation_id"],
        vault_id=vault_id,
        generation=principal.credential_generation,
    )
    return result
