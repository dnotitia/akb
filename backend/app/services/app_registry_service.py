"""System-admin registry operations for app definitions and releases."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.services.app_identity_service import record_app_audit
from app.services.app_rollout_service import manifest_storage_projection, validate_manifest
from app.services.auth_service import AuthenticatedUser
from app.util.text import to_nfc_any


def _json_object(value: Any) -> dict[str, Any]:
    """Decode asyncpg's text representation of a JSONB object."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _canonical(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return json.dumps(
        to_nfc_any(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _as_uuid(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"{field} must be a UUID") from exc


def _require_admin(user: AuthenticatedUser) -> None:
    if not user.is_admin:
        raise ForbiddenError("System administrator permission required")


def _record_registry_error(
    action: str,
    *,
    correlation_id: str,
    user: AuthenticatedUser,
    app_id: uuid.UUID | str | None = None,
) -> None:
    record_app_audit(
        action,
        correlation_id=correlation_id,
        outcome="error",
        reason="rejected",
        actor=user.username,
        actor_id=user.user_id,
        app_id=app_id,
    )


def _app_projection(row: Any, *, replayed: bool | None = None) -> dict[str, Any]:
    result = {
        "id": str(row["id"]),
        "app_key": row["app_key"],
        "display_name": row["display_name"],
        "description": row["description"],
        "metadata": _json_object(row["metadata"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if replayed is not None:
        result["replayed"] = replayed
    return result


def _release_projection(row: Any, *, replayed: bool | None = None) -> dict[str, Any]:
    validated = validate_manifest(
        _json_object(row["manifest"]),
        row["manifest_checksum"],
        version=row["version"],
    )
    result = {
        "id": str(row["id"]),
        "app_id": str(row["app_id"]),
        "version": row["version"],
        "manifest": manifest_storage_projection(validated),
        "manifest_checksum": row["manifest_checksum"],
        "registered_at": row["registered_at"],
    }
    if replayed is not None:
        result["replayed"] = replayed
    return result


async def create_app_definition(
    *,
    app_key: str,
    display_name: str | None,
    description: str | None,
    metadata: dict[str, Any],
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    _require_admin(user)
    app_key = app_key.strip()
    if not app_key:
        raise ValidationError("app_key must not be empty")
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"app-definition:{app_key}",
                )
                existing = await conn.fetchrow(
                    "SELECT * FROM app_definitions WHERE app_key=$1 FOR UPDATE", app_key
                )
                if existing is not None:
                    same = (
                        existing["display_name"] == display_name
                        and existing["description"] == description
                        and _canonical(existing["metadata"] or {}) == _canonical(metadata)
                    )
                    if not same:
                        raise ConflictError("App key is already registered with different identity")
                    result = _app_projection(existing, replayed=True)
                else:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO app_definitions(app_key, display_name, description, metadata)
                        VALUES($1,$2,$3,$4::jsonb)
                        RETURNING *
                        """,
                        app_key,
                        display_name,
                        description,
                        _canonical(metadata),
                    )
                    assert row is not None
                    result = _app_projection(row, replayed=False)
    except asyncpg.UniqueViolationError:
        _record_registry_error(
            "app.registry.create",
            correlation_id=correlation_id,
            user=user,
        )
        raise ConflictError("App key is already registered") from None
    except (ConflictError, ValidationError, NotFoundError):
        _record_registry_error(
            "app.registry.create",
            correlation_id=correlation_id,
            user=user,
        )
        raise
    record_app_audit(
        "app.registry.create",
        correlation_id=correlation_id,
        outcome="ok" if not result.get("replayed") else "replay",
        reason="replayed" if result.get("replayed") else "created",
        actor=user.username,
        actor_id=user.user_id,
        app_id=result["id"],
    )
    return result


async def get_app_definition(
    app_id: uuid.UUID | str, *, user: AuthenticatedUser, correlation_id: str
) -> dict[str, Any]:
    _require_admin(user)
    app_uuid = _as_uuid(app_id, field="app_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM app_definitions WHERE id=$1", app_uuid)
    if row is None:
        _record_registry_error(
            "app.registry.read",
            correlation_id=correlation_id,
            user=user,
            app_id=app_uuid,
        )
        raise NotFoundError("App", "not found")
    result = _app_projection(row)
    record_app_audit(
        "app.registry.read",
        correlation_id=correlation_id,
        outcome="ok",
        reason="read",
        actor=user.username,
        actor_id=user.user_id,
        app_id=app_uuid,
    )
    return result


async def update_app_definition(
    app_id: uuid.UUID | str,
    *,
    fields: dict[str, Any],
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    _require_admin(user)
    app_uuid = _as_uuid(app_id, field="app_id")
    allowed = {"display_name", "description", "metadata"}
    if not fields or set(fields) - allowed:
        _record_registry_error(
            "app.registry.update",
            correlation_id=correlation_id,
            user=user,
            app_id=app_uuid,
        )
        raise ValidationError("Only display_name, description, and metadata may change")
    if "metadata" in fields and not isinstance(fields["metadata"], dict):
        _record_registry_error(
            "app.registry.update",
            correlation_id=correlation_id,
            user=user,
            app_id=app_uuid,
        )
        raise ValidationError("metadata must be an object")
    assignments: list[str] = []
    args: list[Any] = [app_uuid]
    for name in ("display_name", "description", "metadata"):
        if name not in fields:
            continue
        assignments.append(f"{name}=${len(args)+1}{'::jsonb' if name == 'metadata' else ''}")
        value = fields[name]
        args.append(_canonical(value) if name == "metadata" else value)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"UPDATE app_definitions SET {', '.join(assignments)} WHERE id=$1 RETURNING *",
                *args,
            )
    if row is None:
        _record_registry_error(
            "app.registry.update",
            correlation_id=correlation_id,
            user=user,
            app_id=app_uuid,
        )
        raise NotFoundError("App", "not found")
    result = _app_projection(row, replayed=False)
    record_app_audit(
        "app.registry.update",
        correlation_id=correlation_id,
        outcome="ok",
        reason="updated",
        actor=user.username,
        actor_id=user.user_id,
        app_id=app_uuid,
    )
    return result


async def create_app_release(
    app_id: uuid.UUID | str,
    *,
    version: str,
    manifest: dict[str, Any],
    manifest_checksum: str,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    _require_admin(user)
    app_uuid = _as_uuid(app_id, field="app_id")
    if not isinstance(version, str):
        version = ""
    else:
        version = version.strip()
    if not version:
        _record_registry_error(
            "app.registry.release.create",
            correlation_id=correlation_id,
            user=user,
            app_id=app_uuid,
        )
        raise ValidationError("version must not be empty")
    try:
        normalized = validate_manifest(manifest, manifest_checksum, version=version)
    except (ConflictError, ValidationError):
        _record_registry_error(
            "app.registry.release.create",
            correlation_id=correlation_id,
            user=user,
            app_id=app_uuid,
        )
        raise
    checksum = normalized["checksum"]
    normalized_manifest = manifest_storage_projection(normalized)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"app-release:{app_uuid}:{version}",
            )
            app = await conn.fetchrow(
                "SELECT id, app_key FROM app_definitions WHERE id=$1",
                app_uuid,
            )
            if app is None:
                _record_registry_error(
                    "app.registry.release.create",
                    correlation_id=correlation_id,
                    user=user,
                    app_id=app_uuid,
                )
                raise NotFoundError("App", "not found")
            if normalized_manifest["app_key"] != app["app_key"]:
                _record_registry_error(
                    "app.registry.release.create",
                    correlation_id=correlation_id,
                    user=user,
                    app_id=app_uuid,
                )
                raise ConflictError("Release manifest app_key does not match the app definition")
            existing = await conn.fetchrow(
                "SELECT * FROM app_releases WHERE app_id=$1 AND version=$2 FOR UPDATE",
                app_uuid,
                version,
            )
            if existing is not None:
                same = (
                    existing["manifest_checksum"] == checksum
                    and _canonical(existing["manifest"]) == _canonical(normalized_manifest)
                )
                if not same:
                    _record_registry_error(
                        "app.registry.release.create",
                        correlation_id=correlation_id,
                        user=user,
                        app_id=app_uuid,
                    )
                    raise ConflictError("Release version is already registered with different content")
                result = _release_projection(existing, replayed=True)
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO app_releases(app_id, version, manifest, manifest_checksum)
                    VALUES($1,$2,$3::jsonb,$4)
                    RETURNING *
                    """,
                    app_uuid,
                    version,
                    _canonical(normalized_manifest),
                    checksum,
                )
                assert row is not None
                result = _release_projection(row, replayed=False)
    record_app_audit(
        "app.registry.release.create",
        correlation_id=correlation_id,
        outcome="ok" if not result.get("replayed") else "replay",
        reason="replayed" if result.get("replayed") else "created",
        actor=user.username,
        actor_id=user.user_id,
        app_id=app_uuid,
    )
    return result


async def get_app_release(
    app_id: uuid.UUID | str,
    release_id: uuid.UUID | str,
    *,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    _require_admin(user)
    app_uuid = _as_uuid(app_id, field="app_id")
    release_uuid = _as_uuid(release_id, field="release_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM app_releases WHERE app_id=$1 AND id=$2",
            app_uuid,
            release_uuid,
        )
    if row is None:
        _record_registry_error(
            "app.registry.release.read",
            correlation_id=correlation_id,
            user=user,
            app_id=app_uuid,
        )
        raise NotFoundError("Release", "not found")
    result = _release_projection(row)
    record_app_audit(
        "app.registry.release.read",
        correlation_id=correlation_id,
        outcome="ok",
        reason="read",
        actor=user.username,
        actor_id=user.user_id,
        app_id=app_uuid,
    )
    return result
