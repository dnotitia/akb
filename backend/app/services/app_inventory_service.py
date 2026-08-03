"""App control-plane inventory, observed state, and rollout snapshots.

The desired registry remains the source of truth for what should exist.  This
service only projects that state, stores the newest worker observation, and
freezes rollout membership; it does not execute migrations or backfills.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.app_identity_service import (
    AppPrincipal,
    authorize_app_request,
    record_app_audit,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
INVENTORY_SCOPES = frozenset({"admin", "app"})
INSTALLATION_LIFECYCLES = frozenset(
    {"installing", "active", "upgrading", "blocked", "uninstalled"}
)
TARGET_STATES = frozenset(
    {"pending", "running", "applied", "replayed", "failed", "skipped", "denied"}
)

_CURSOR_VERSION = 1
_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SAFE_FINGERPRINT = re.compile(r"^[0-9A-Fa-f]{8,256}$")
_SAFE_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$")
_SAFE_ISO = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _cursor_secret() -> bytes:
    configured = (settings.app_token_secret or settings.jwt_secret or "").encode()
    # Unit-only configurations may intentionally leave auth secrets blank.  A
    # deterministic fallback keeps cursors opaque and, more importantly,
    # still binds them to their signed payload rather than exposing JSON.
    return configured or b"akb-app-inventory-cursor-v1"


def _as_uuid(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"{field} must be a UUID") from exc


def _normalize_datetime(value: datetime | str | None, *, field: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)


def normalize_page_size(limit: int | None) -> int:
    value = DEFAULT_PAGE_SIZE if limit is None else limit
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("limit must be an integer")
    if value < 1 or value > MAX_PAGE_SIZE:
        raise ValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return value


def normalize_lifecycle(lifecycle: str | None) -> str | None:
    if lifecycle is None:
        return None
    if lifecycle not in INSTALLATION_LIFECYCLES:
        raise ValidationError("lifecycle filter is invalid")
    return lifecycle


def _cursor_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def encode_inventory_cursor(
    *,
    app_id: uuid.UUID,
    scope: str,
    limit: int,
    lifecycle: str | None,
    boundary: datetime,
    last_created_at: datetime,
    last_installation_id: uuid.UUID,
) -> str:
    """Create an opaque, signed cursor bound to one inventory traversal."""
    if scope not in INVENTORY_SCOPES:
        raise ValidationError("inventory cursor scope is invalid")
    payload = {
        "v": _CURSOR_VERSION,
        "app": str(app_id),
        "scope": scope,
        "limit": normalize_page_size(limit),
        "lifecycle": lifecycle,
        "boundary": boundary.astimezone(timezone.utc).isoformat(),
        "last": last_created_at.astimezone(timezone.utc).isoformat(),
        "id": str(last_installation_id),
    }
    raw = _cursor_payload_bytes(payload)
    signature = hmac.new(_cursor_secret(), raw, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(raw + b"." + signature).rstrip(b"=")
    return encoded.decode("ascii")


def decode_inventory_cursor(
    cursor: str,
    *,
    app_id: uuid.UUID,
    scope: str,
    limit: int,
    lifecycle: str | None,
) -> dict[str, Any]:
    """Decode and validate a cursor without exposing its payload on failure."""
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        signature_size = hashlib.sha256().digest_size
        separator_index = len(decoded) - signature_size - 1
        if separator_index < 1 or decoded[separator_index] != ord("."):
            raise ValueError
        raw = decoded[:separator_index]
        supplied_signature = decoded[separator_index + 1 :]
        expected_signature = hmac.new(_cursor_secret(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError
        payload = json.loads(raw.decode("ascii"))
        if not isinstance(payload, dict):
            raise ValueError
        if (
            payload.get("v") != _CURSOR_VERSION
            or payload.get("app") != str(app_id)
            or payload.get("scope") != scope
            or payload.get("limit") != normalize_page_size(limit)
            or payload.get("lifecycle") != lifecycle
        ):
            raise ValueError
        boundary_value = payload.get("boundary")
        last_created_at_value = payload.get("last")
        if not isinstance(boundary_value, str) or not isinstance(last_created_at_value, str):
            raise ValueError
        boundary = _normalize_datetime(boundary_value, field="cursor")
        last_created_at = _normalize_datetime(last_created_at_value, field="cursor")
        cursor_id = payload.get("id")
        if not isinstance(cursor_id, str):
            raise ValueError
        last_id = _as_uuid(cursor_id, field="cursor")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError):
        raise ValidationError("Invalid inventory cursor") from None
    return {
        "boundary": boundary,
        "last_created_at": last_created_at,
        "last_installation_id": last_id,
    }


def _safe_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_CODE.fullmatch(value) else None


def _safe_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc).isoformat()
    if not isinstance(value, str):
        return None
    return value if len(value) <= 64 and _SAFE_ISO.fullmatch(value) else None


def _safe_fingerprint(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_FINGERPRINT.fullmatch(value) else None


def _safe_release(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_RELEASE.fullmatch(value) else None


def sanitize_checkpoint(value: Any) -> dict[str, Any]:
    """Keep only bounded operational checkpoint fields.

    Unknown keys are intentionally discarded.  This prevents worker request
    bodies, credentials, and arbitrary provider metadata from becoming part of
    the inventory read model.
    """
    value = _json_object(value)
    result: dict[str, Any] = {}
    for key in ("phase", "step", "code"):
        safe = _safe_code(value.get(key))
        if safe is not None:
            result[key] = safe
    for key in ("completed", "total", "attempt"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10**9:
            result[key] = item
    for key in ("at", "updated_at"):
        safe = _safe_iso(value.get(key))
        if safe is not None:
            result[key] = safe
    return result


def sanitize_recent_error(value: Any) -> dict[str, Any] | None:
    """Return a bounded error summary without free-form payload text."""
    value = _json_object(value)
    result: dict[str, Any] = {}
    code = _safe_code(value.get("code"))
    if code is not None:
        result["code"] = code
    phase = _safe_code(value.get("phase"))
    if phase is not None:
        result["phase"] = phase
    at = _safe_iso(value.get("at") or value.get("occurred_at"))
    if at is not None:
        result["at"] = at
    if isinstance(value.get("retryable"), bool):
        result["retryable"] = value["retryable"]
    return result or None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def expected_schema_fingerprint(manifest: Any) -> str | None:
    """Read the optional expected schema fingerprint from a release manifest."""
    manifest = _json_object(manifest)
    candidates = [
        manifest.get("expected_schema_fingerprint"),
        manifest.get("schema_fingerprint"),
        _json_object(manifest.get("schema")).get("fingerprint"),
        _json_object(manifest.get("schema")).get("expected_fingerprint"),
    ]
    for candidate in candidates:
        value = _safe_fingerprint(candidate)
        if value is not None:
            return value
    return None


def classify_drift(row: Any) -> dict[str, Any]:
    """Classify release, schema, and grant drift independently."""
    observed_generation = row.get("observed_generation") if hasattr(row, "get") else row["observed_generation"]
    observed_exists = observed_generation is not None
    desired_release_id = row.get("desired_release_id") if hasattr(row, "get") else row["desired_release_id"]
    observed_release_id = row.get("observed_release_id") if hasattr(row, "get") else row["observed_release_id"]
    desired_version = row.get("desired_version") if hasattr(row, "get") else row["desired_version"]
    observed_version = row.get("observed_release_version") if hasattr(row, "get") else row["observed_release_version"]
    expected_schema = expected_schema_fingerprint(
        row.get("desired_manifest") if hasattr(row, "get") else row["desired_manifest"]
    )
    observed_schema = _safe_fingerprint(
        row.get("schema_fingerprint") if hasattr(row, "get") else row["schema_fingerprint"]
    )
    desired_grant_generation = row.get("grant_generation") if hasattr(row, "get") else row["grant_generation"]
    observed_grant_generation = (
        row.get("observed_grant_generation")
        if hasattr(row, "get")
        else row["observed_grant_generation"]
    )

    if not observed_exists:
        release_status = schema_status = grant_status = "unknown"
    else:
        if desired_release_id is None:
            release_status = "unknown"
        elif observed_release_id is not None:
            release_status = "match" if observed_release_id == desired_release_id else "mismatch"
        elif observed_version is not None and desired_version is not None:
            release_status = "match" if observed_version == desired_version else "mismatch"
        else:
            release_status = "unknown"

        if expected_schema is None or observed_schema is None:
            schema_status = "unknown"
        else:
            schema_status = "match" if expected_schema == observed_schema else "mismatch"

        if observed_grant_generation is None or desired_grant_generation is None:
            grant_status = "unknown"
        else:
            grant_status = (
                "match"
                if observed_grant_generation == desired_grant_generation
                else "mismatch"
            )

    dimensions = {
        "release": {
            "status": release_status,
            "desired": desired_version,
            "observed": observed_version,
        },
        "schema": {
            "status": schema_status,
            "expected": expected_schema,
            "observed": observed_schema,
        },
        "grant": {
            "status": grant_status,
            "desired_generation": desired_grant_generation,
            "observed_generation": observed_grant_generation,
        },
    }
    reasons = [
        f"{dimension}_mismatch"
        for dimension, data in dimensions.items()
        if data["status"] == "mismatch"
    ]
    unknown = [
        dimension
        for dimension, data in dimensions.items()
        if data["status"] == "unknown"
    ]
    overall = "drifted" if reasons else "unknown" if unknown else "in_sync"
    return {
        "release": dimensions["release"],
        "schema": dimensions["schema"],
        "grant": dimensions["grant"],
        "overall": overall,
        "reasons": reasons,
        "unknown_dimensions": unknown,
    }


def _release_payload(release_id: Any, version: Any) -> dict[str, Any] | None:
    if release_id is None and version is None:
        return None
    return {
        "id": str(release_id) if release_id is not None else None,
        "version": version,
    }


def _observed_payload(row: Any) -> dict[str, Any] | None:
    if row["observed_generation"] is None:
        return None
    return {
        "generation": row["observed_generation"],
        "observed_at": row["observed_at"].isoformat() if row["observed_at"] else None,
        "release": _release_payload(
            row["observed_release_id"],
            row["observed_release_version"],
        ),
        "schema_fingerprint": _safe_fingerprint(row["schema_fingerprint"]),
        "grant_generation": row["observed_grant_generation"],
        "checkpoint": sanitize_checkpoint(row["checkpoint"]),
        "recent_error": sanitize_recent_error(row["recent_error"]),
    }


def project_inventory_item(row: Any) -> dict[str, Any]:
    """Project one DB row without issuer, provenance, resource metadata, or payloads."""
    drift = classify_drift(row)
    latest_grant = None
    if row["latest_grant_generation"] is not None:
        capabilities = row["latest_grant_capabilities"] or []
        latest_grant = {
            "generation": row["latest_grant_generation"],
            "status": row["latest_grant_status"],
            "capabilities": sorted(
                capability for capability in capabilities if isinstance(capability, str)
            ),
        }
    latest_active_grant = None
    if row.get("latest_active_grant_generation") is not None:
        active_capabilities = row["latest_active_grant_capabilities"] or []
        latest_active_grant = {
            "generation": row["latest_active_grant_generation"],
            "status": row["latest_active_grant_status"],
            "capabilities": sorted(
                capability
                for capability in active_capabilities
                if isinstance(capability, str)
            ),
        }
    return {
        "installation_id": str(row["installation_id"]),
        "app_id": str(row["app_id"]),
        "vault_id": str(row["vault_id"]),
        "vault_name": row["vault_name"],
        "lifecycle": row["lifecycle"],
        "desired_release": _release_payload(
            row["desired_release_id"],
            row["desired_version"],
        ),
        "current_release": _release_payload(
            row["current_release_id"],
            row["current_version"],
        ),
        "observed": _observed_payload(row),
        "latest_grant": latest_grant,
        "latest_active_grant": latest_active_grant,
        "grant_generation": row["grant_generation"],
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


_INVENTORY_SELECT = """
    SELECT
        installation.id AS installation_id,
        installation.app_id,
        installation.vault_id,
        vault.name AS vault_name,
        installation.lifecycle,
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
        installation.created_at,
        installation.updated_at
      FROM vault_app_installations AS installation
      JOIN vaults AS vault ON vault.id = installation.vault_id
      LEFT JOIN app_releases AS desired ON desired.id = installation.desired_release_id
      LEFT JOIN app_releases AS current_release ON current_release.id = installation.current_release_id
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
"""


async def list_inventory(
    app_id: uuid.UUID | str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    lifecycle: str | None = None,
    scope: str = "admin",
    capability: str | None = None,
) -> dict[str, Any]:
    """List an app's inventory with a stable, bounded cursor traversal."""
    app_id = _as_uuid(app_id, field="app_id")
    if scope not in INVENTORY_SCOPES:
        raise ValidationError("inventory scope is invalid")
    limit = normalize_page_size(limit)
    lifecycle = normalize_lifecycle(lifecycle)
    if capability is not None and capability != "inventory:read":
        raise ValidationError("inventory capability is invalid")

    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM app_definitions WHERE id = $1", app_id):
            raise NotFoundError("App", "not found")

        if cursor is None:
            boundary = await conn.fetchval("SELECT CURRENT_TIMESTAMP")
            last_created_at = None
            last_installation_id = None
        else:
            decoded = decode_inventory_cursor(
                cursor,
                app_id=app_id,
                scope=scope,
                limit=limit,
                lifecycle=lifecycle,
            )
            boundary = decoded["boundary"]
            last_created_at = decoded["last_created_at"]
            last_installation_id = decoded["last_installation_id"]

        rows = await conn.fetch(
            _INVENTORY_SELECT
            + """
             WHERE installation.app_id = $1
               AND installation.created_at <= $2
               AND ($3::text IS NULL OR installation.lifecycle = $3)
               AND (
                   $4::timestamptz IS NULL
                   OR installation.created_at > $4
                   OR (
                       installation.created_at = $4
                       AND installation.id > $5
                   )
               )
               AND (
                   $6::text IS NULL
                   OR EXISTS (
                       SELECT 1
                         FROM installation_grants AS visible_grant
                        WHERE visible_grant.installation_id = installation.id
                          AND visible_grant.generation = installation.grant_generation
                          AND visible_grant.status = 'active'
                          AND $6 = ANY(visible_grant.capabilities)
                   )
               )
             ORDER BY installation.created_at ASC, installation.id ASC
             LIMIT $7
            """,
            app_id,
            boundary,
            lifecycle,
            last_created_at,
            last_installation_id,
            capability,
            limit + 1,
        )

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        tail = page[-1]
        next_cursor = encode_inventory_cursor(
            app_id=app_id,
            scope=scope,
            limit=limit,
            lifecycle=lifecycle,
            boundary=boundary,
            last_created_at=tail["created_at"],
            last_installation_id=tail["installation_id"],
        )
    return {
        "items": [project_inventory_item(row) for row in page],
        "next_cursor": next_cursor,
    }


async def report_observed_state(
    installation_id: uuid.UUID | str,
    *,
    observed_generation: int,
    observed_at: datetime | str | None = None,
    observed_release_id: uuid.UUID | str | None = None,
    observed_release_version: str | None = None,
    schema_fingerprint: str | None = None,
    observed_grant_generation: int | None = None,
    checkpoint: Any = None,
    recent_error: Any = None,
    app_id: uuid.UUID | str | None = None,
    principal: AppPrincipal | None = None,
    correlation_id: str = "app-inventory",
    actor: str | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Persist the newest worker report for one installation."""
    installation_id = _as_uuid(installation_id, field="installation_id")
    if isinstance(observed_generation, bool) or not isinstance(observed_generation, int):
        raise ValidationError("observed_generation must be an integer")
    if observed_generation < 0:
        raise ValidationError("observed_generation must not be negative")
    if observed_grant_generation is not None and (
        isinstance(observed_grant_generation, bool)
        or not isinstance(observed_grant_generation, int)
        or observed_grant_generation < 0
    ):
        raise ValidationError("observed_grant_generation must be a non-negative integer")
    observed_at = _normalize_datetime(observed_at, field="observed_at")
    app_uuid = _as_uuid(app_id, field="app_id") if app_id is not None else None
    release_uuid = (
        _as_uuid(observed_release_id, field="observed_release_id")
        if observed_release_id is not None
        else None
    )
    release_version = _safe_release(observed_release_version)
    if observed_release_version is not None and release_version is None:
        raise ValidationError("observed_release_version is invalid")
    fingerprint = _safe_fingerprint(schema_fingerprint)
    if schema_fingerprint is not None and fingerprint is None:
        raise ValidationError("schema_fingerprint is invalid")
    clean_checkpoint = sanitize_checkpoint(checkpoint)
    clean_error = sanitize_recent_error(recent_error)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            installation = await conn.fetchrow(
                """
                SELECT app_id, vault_id
                  FROM vault_app_installations
                 WHERE id = $1
                """,
                installation_id,
            )
            if installation is None or (
                app_uuid is not None and installation["app_id"] != app_uuid
            ):
                raise NotFoundError("Installation", "not found")
            if principal is not None and installation["app_id"] != principal.app_id:
                raise NotFoundError("Installation", "not found")
            if release_uuid is not None and not await conn.fetchval(
                "SELECT 1 FROM app_releases WHERE app_id = $1 AND id = $2",
                installation["app_id"],
                release_uuid,
            ):
                raise ValidationError("observed_release_id is not registered for this app")
            if principal is not None:
                await authorize_app_request(
                    principal,
                    vault_id=installation["vault_id"],
                    capability="inventory:read",
                    correlation_id=correlation_id,
                    conn=conn,
                )

            row = await conn.fetchrow(
                """
                INSERT INTO app_installation_observed_states (
                    installation_id,
                    app_id,
                    vault_id,
                    observed_generation,
                    observed_at,
                    observed_release_id,
                    observed_release_version,
                    schema_fingerprint,
                    observed_grant_generation,
                    checkpoint,
                    recent_error
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
                ON CONFLICT (installation_id) DO UPDATE
                    SET observed_generation = EXCLUDED.observed_generation,
                        observed_at = EXCLUDED.observed_at,
                        observed_release_id = EXCLUDED.observed_release_id,
                        observed_release_version = EXCLUDED.observed_release_version,
                        schema_fingerprint = EXCLUDED.schema_fingerprint,
                        observed_grant_generation = EXCLUDED.observed_grant_generation,
                        checkpoint = EXCLUDED.checkpoint,
                        recent_error = EXCLUDED.recent_error,
                        received_at = NOW()
                  WHERE EXCLUDED.observed_generation >= app_installation_observed_states.observed_generation
                    AND EXCLUDED.observed_at >= app_installation_observed_states.observed_at
                RETURNING *
                """,
                installation_id,
                installation["app_id"],
                installation["vault_id"],
                observed_generation,
                observed_at,
                release_uuid,
                release_version,
                fingerprint,
                observed_grant_generation,
                json.dumps(clean_checkpoint),
                json.dumps(clean_error) if clean_error is not None else None,
            )
            accepted = row is not None
            if row is None:
                row = await conn.fetchrow(
                    "SELECT * FROM app_installation_observed_states WHERE installation_id = $1",
                    installation_id,
                )

    if row is None:  # pragma: no cover - guarded by the installation lookup
        raise ConflictError("Observed report was not stored")
    return {
        "accepted": accepted,
        "installation_id": str(installation_id),
        "observed_generation": row["observed_generation"],
        "observed_at": row["observed_at"].isoformat(),
    }


async def create_rollout_snapshot(
    app_id: uuid.UUID | str,
    *,
    requested_by_kind: str = "admin",
    correlation_id: str = "app-inventory",
    actor: str | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Create and seal an immutable desired-release membership snapshot."""
    app_id = _as_uuid(app_id, field="app_id")
    if requested_by_kind not in {"admin", "app"}:
        raise ValidationError("requested_by_kind is invalid")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT 1 FROM app_definitions WHERE id = $1", app_id):
                raise NotFoundError("App", "not found")
            snapshot = await conn.fetchrow(
                """
                INSERT INTO app_rollout_snapshots (app_id, requested_by_kind)
                VALUES ($1, $2)
                RETURNING id, app_id, created_at
                """,
                app_id,
                requested_by_kind,
            )
            await conn.execute(
                """
                INSERT INTO app_rollout_snapshot_targets (
                    snapshot_id,
                    app_id,
                    installation_id,
                    vault_id,
                    desired_release_id,
                    current_release_id,
                    baseline_grant_generation
                )
                SELECT $1,
                       installation.app_id,
                       installation.id,
                       installation.vault_id,
                       installation.desired_release_id,
                       installation.current_release_id,
                       installation.grant_generation
                  FROM vault_app_installations AS installation
                 WHERE installation.app_id = $2
                   AND installation.desired_release_id IS NOT NULL
                """,
                snapshot["id"],
                app_id,
            )
            await conn.execute(
                """
                UPDATE app_rollout_snapshot_targets AS target
                   SET state = CASE
                       WHEN installation.lifecycle <> 'active'
                           THEN 'denied'
                       WHEN grant_row.generation IS NULL
                            OR grant_row.status <> 'active'
                            OR grant_row.generation <> installation.grant_generation
                           THEN 'denied'
                       WHEN observed.observed_generation IS NULL
                           THEN 'skipped'
                       WHEN observed.observed_grant_generation IS NULL
                            OR observed.observed_grant_generation <> installation.grant_generation
                           THEN 'skipped'
                       WHEN installation.current_release_id IS NOT NULL
                            AND (
                                observed.observed_release_id IS NULL
                                OR observed.observed_release_id <> installation.current_release_id
                            )
                           THEN 'skipped'
                       ELSE 'pending'
                   END,
                       reason_code = CASE
                       WHEN installation.lifecycle <> 'active'
                           THEN 'installation_inactive'
                       WHEN grant_row.generation IS NULL
                            OR grant_row.status <> 'active'
                            OR grant_row.generation <> installation.grant_generation
                           THEN 'grant_revoked_or_missing'
                       WHEN observed.observed_generation IS NULL
                           THEN 'observed_state_missing'
                       WHEN observed.observed_grant_generation IS NULL
                            OR observed.observed_grant_generation <> installation.grant_generation
                           THEN 'observed_grant_stale'
                       WHEN installation.current_release_id IS NOT NULL
                            AND (
                                observed.observed_release_id IS NULL
                                OR observed.observed_release_id <> installation.current_release_id
                            )
                           THEN 'observed_release_stale'
                       ELSE NULL
                   END
                  FROM vault_app_installations AS installation
                  LEFT JOIN LATERAL (
                      SELECT grant_row.generation, grant_row.status
                        FROM installation_grants AS grant_row
                       WHERE grant_row.installation_id = installation.id
                         AND grant_row.generation = installation.grant_generation
                       LIMIT 1
                  ) AS grant_row ON TRUE
                  LEFT JOIN app_installation_observed_states AS observed
                    ON observed.installation_id = installation.id
                 WHERE target.snapshot_id = $1
                   AND target.installation_id = installation.id
                """,
                snapshot["id"],
            )
            target_count = await conn.fetchval(
                "SELECT count(*) FROM app_rollout_snapshot_targets WHERE snapshot_id = $1",
                snapshot["id"],
            )
            sealed = await conn.fetchrow(
                """
                UPDATE app_rollout_snapshots
                   SET sealed_at = NOW()
                 WHERE id = $1 AND sealed_at IS NULL
                RETURNING id, app_id, created_at, sealed_at, requested_by_kind
                """,
                snapshot["id"],
            )

    if sealed is None:  # pragma: no cover - the transaction owns the row
        raise ConflictError("Rollout snapshot could not be sealed")
    record_app_audit(
        "app.rollout.snapshot.create",
        correlation_id=correlation_id,
        outcome="ok",
        reason="sealed",
        actor=actor,
        actor_id=actor_id,
        app_id=app_id,
    )
    return {
        "snapshot_id": str(sealed["id"]),
        "app_id": str(sealed["app_id"]),
        "created_at": sealed["created_at"].isoformat(),
        "sealed_at": sealed["sealed_at"].isoformat(),
        "requested_by_kind": sealed["requested_by_kind"],
        "target_count": int(target_count or 0),
    }


async def get_rollout_snapshot(
    app_id: uuid.UUID | str,
    snapshot_id: uuid.UUID | str,
    *,
    target_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Read a snapshot and its frozen targets within one app scope."""
    app_id = _as_uuid(app_id, field="app_id")
    snapshot_id = _as_uuid(snapshot_id, field="snapshot_id")
    target_uuid = _as_uuid(target_id, field="target_id") if target_id is not None else None
    pool = await get_pool()
    async with pool.acquire() as conn:
        snapshot = await conn.fetchrow(
            """
            SELECT id, app_id, created_at, sealed_at, requested_by_kind
              FROM app_rollout_snapshots
             WHERE id = $1 AND app_id = $2
            """,
            snapshot_id,
            app_id,
        )
        if snapshot is None:
            raise NotFoundError("Rollout snapshot", "not found")
        rows = await conn.fetch(
            """
            SELECT target.id,
                   target.installation_id,
                   target.vault_id,
                   target.desired_release_id,
                   desired.version AS desired_version,
                   target.current_release_id,
                   current_release.version AS current_version,
                   target.baseline_grant_generation,
                   target.state,
                   target.reason_code,
                   target.created_at,
                   target.updated_at
              FROM app_rollout_snapshot_targets AS target
              LEFT JOIN app_releases AS desired
                ON desired.id = target.desired_release_id
              LEFT JOIN app_releases AS current_release
                ON current_release.id = target.current_release_id
             WHERE target.snapshot_id = $1
               AND ($2::uuid IS NULL OR target.id = $2)
             ORDER BY target.created_at ASC, target.id ASC
            """,
            snapshot_id,
            target_uuid,
        )

    targets = [
        {
            "target_id": str(row["id"]),
            "installation_id": str(row["installation_id"]),
            "vault_id": str(row["vault_id"]),
            "desired_release": _release_payload(
                row["desired_release_id"],
                row["desired_version"],
            ),
            "current_release": _release_payload(
                row["current_release_id"],
                row["current_version"],
            ),
            "baseline_grant_generation": row["baseline_grant_generation"],
            "state": row["state"],
            "reason_code": row["reason_code"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
        for row in rows
    ]
    return {
        "snapshot_id": str(snapshot["id"]),
        "app_id": str(snapshot["app_id"]),
        "created_at": snapshot["created_at"].isoformat(),
        "sealed_at": snapshot["sealed_at"].isoformat() if snapshot["sealed_at"] else None,
        "requested_by_kind": snapshot["requested_by_kind"],
        "target_count": len(targets),
        "targets": targets,
    }


async def evaluate_rollout_target(
    app_id: uuid.UUID | str,
    snapshot_id: uuid.UUID | str,
    target_id: uuid.UUID | str,
    *,
    correlation_id: str = "app-inventory",
    actor: str | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Recheck live eligibility and mark a target running when safe.

    This is the pre-execution seam only.  It never performs migration or
    backfill work; an eligible target is merely moved to ``running`` for the
    later AKB-126 executor.
    """
    app_id = _as_uuid(app_id, field="app_id")
    snapshot_id = _as_uuid(snapshot_id, field="snapshot_id")
    target_id = _as_uuid(target_id, field="target_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow(
                """
                SELECT target.*, snapshot.created_at AS snapshot_created_at
                  FROM app_rollout_snapshot_targets AS target
                  JOIN app_rollout_snapshots AS snapshot
                    ON snapshot.id = target.snapshot_id
                 WHERE target.id = $1
                   AND target.snapshot_id = $2
                   AND target.app_id = $3
                 FOR UPDATE
                """,
                target_id,
                snapshot_id,
                app_id,
            )
            if target is None:
                raise NotFoundError("Rollout target", "not found")
            if target["state"] != "pending":
                return {
                    "target_id": str(target["id"]),
                    "eligible": target["state"] == "running",
                    "executed": False,
                    "state": target["state"],
                    "reason_code": target["reason_code"],
                }

            installation = await conn.fetchrow(
                """
                SELECT id, app_id, vault_id, lifecycle,
                       desired_release_id, current_release_id, grant_generation
                  FROM vault_app_installations
                 WHERE id = $1
                """,
                target["installation_id"],
            )
            state = "running"
            reason = None
            if installation is None:
                state, reason = "denied", "installation_missing"
            elif installation["app_id"] != app_id:
                state, reason = "denied", "installation_scope_mismatch"
            elif installation["lifecycle"] != "active":
                state, reason = "denied", "installation_inactive"
            elif (
                installation["desired_release_id"] != target["desired_release_id"]
                or installation["current_release_id"] != target["current_release_id"]
            ):
                state, reason = "skipped", "release_changed"
            else:
                latest_grant = await conn.fetchrow(
                    """
                    SELECT status, generation
                      FROM installation_grants
                     WHERE installation_id = $1
                     ORDER BY generation DESC
                     LIMIT 1
                    """,
                    installation["id"],
                )
                if (
                    latest_grant is None
                    or latest_grant["status"] != "active"
                    or latest_grant["generation"] != installation["grant_generation"]
                ):
                    state, reason = "denied", "grant_revoked_or_missing"
                elif installation["grant_generation"] != target["baseline_grant_generation"]:
                    state, reason = "skipped", "grant_generation_stale"
                else:
                    observed = await conn.fetchrow(
                        """
                        SELECT observed_release_id, observed_grant_generation
                          FROM app_installation_observed_states
                         WHERE installation_id = $1
                        """,
                        installation["id"],
                    )
                    if observed is None:
                        state, reason = "skipped", "observed_state_missing"
                    elif (
                        observed["observed_grant_generation"] is None
                        or observed["observed_grant_generation"]
                        != installation["grant_generation"]
                    ):
                        state, reason = "skipped", "observed_grant_stale"
                    elif (
                        installation["current_release_id"] is not None
                        and (
                            observed["observed_release_id"] is None
                            or observed["observed_release_id"]
                            != installation["current_release_id"]
                        )
                    ):
                        state, reason = "skipped", "observed_release_stale"

            await conn.execute(
                """
                UPDATE app_rollout_snapshot_targets
                   SET state = $2, reason_code = $3
                 WHERE id = $1
                """,
                target_id,
                state,
                reason,
            )

    record_app_audit(
        "app.rollout.target.eligibility",
        correlation_id=correlation_id,
        outcome="ok" if state == "running" else "error",
        reason=reason or "eligible",
        actor=actor,
        actor_id=actor_id,
        app_id=app_id,
    )
    return {
        "target_id": str(target_id),
        "eligible": state == "running",
        "executed": False,
        "state": state,
        "reason_code": reason,
    }


# Explicit aliases keep the service boundary discoverable to callers that use
# the noun from the issue document rather than the shorter internal names.
list_app_inventory = list_inventory
upsert_observed_state = report_observed_state
create_rollout_target_snapshot = create_rollout_snapshot
get_rollout_target_snapshot = get_rollout_snapshot
evaluate_target_eligibility = evaluate_rollout_target
