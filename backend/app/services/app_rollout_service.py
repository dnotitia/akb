"""App release rollout contract, request surface, and durable projection.

This module is intentionally narrow: release manifests are validated here,
state-changing requests are assembled in one PostgreSQL transaction, and the
worker in :mod:`app_rollout_worker` consumes only the ledger produced here.
No manifest or SQL payload is returned from a public route.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.services.app_identity_service import (
    AppPrincipal,
    authorize_app_capability,
    record_app_audit,
)
from app.services.app_inventory_service import sanitize_checkpoint
from app.services.auth_service import AuthenticatedUser
from app.util.text import to_nfc_any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PHASES = {"expand": 0, "backfill": 1, "enforce": 2, "contract": 3}
_ALLOWED = {"create_table", "add_column", "add_index", "backfill_column", "set_not_null"}
_REASON = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        to_nfc_any(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _as_uuid(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"{field} must be a UUID") from exc


def _hex(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value.lower()):
        raise ValidationError(f"{field} must be a SHA-256 checksum")
    return value.lower()


def _step_without_checksum(step: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in step.items() if key not in {"checksum", "sha256"}}


def _manifest_without_checksum(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"checksum", "manifest_checksum", "sha256"}
    }


def _operation_payload(step: dict[str, Any]) -> dict[str, Any]:
    payload = step.get("payload")
    if payload is None:
        return {
            key: value
            for key, value in step.items()
            if key not in {"id", "phase", "op", "operation", "checksum", "sha256"}
        }
    if not isinstance(payload, dict):
        raise ValidationError("Manifest step payload must be an object")
    return dict(payload)


def _reject_unlisted_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise ValidationError("Manifest operation contains an unsupported field")


def _table_name(payload: dict[str, Any]) -> str:
    value = payload.get("table", payload.get("table_name"))
    if not isinstance(value, str) or not value or not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        raise ValidationError("Manifest table must be a safe table name")
    return value


def _column_name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        raise ValidationError("Manifest column must be a safe column name")
    return value


def _column_spec(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Manifest column must be an object")
    raw = payload.get("column")
    if isinstance(raw, dict):
        spec = dict(raw)
    elif isinstance(raw, str):
        spec = {"name": raw}
    else:
        spec = {key: payload[key] for key in ("name", "type", "required", "nullable") if key in payload}
    if "name" not in spec:
        raise ValidationError("Manifest column is required")
    if set(spec) - {"name", "type", "required", "nullable"}:
        raise ValidationError("Manifest column contains an unsupported field")
    spec["name"] = _column_name(spec["name"])
    if spec.get("required") or spec.get("nullable") is False:
        raise ValidationError("v1 add_column only permits nullable columns")
    if "default" in spec or "check" in spec or "references" in spec or spec.get("unique"):
        raise ValidationError("Manifest column contains a forbidden constraint")
    spec["type"] = spec.get("type", "text")
    if not isinstance(spec["type"], str):
        raise ValidationError("Manifest column type must be a string")
    return spec


def _normalize_step(step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ValidationError(f"Manifest step {index + 1} must be an object")
    step_id = step.get("id")
    if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
        raise ValidationError("Manifest step id is invalid")
    phase = step.get("phase")
    if phase not in _PHASES:
        raise ValidationError("Manifest step phase is invalid")
    operation = step.get("operation", step.get("op"))
    if not isinstance(operation, str):
        raise ValidationError("Manifest step operation is required")
    operation = operation.strip().lower().replace("-", "_")
    if phase == "contract" or operation not in _ALLOWED:
        raise ValidationError("Manifest contains an unsupported rollout operation")
    expected_phase = {
        "create_table": "expand",
        "add_column": "expand",
        "add_index": "expand",
        "backfill_column": "backfill",
        "set_not_null": "enforce",
    }[operation]
    if phase != expected_phase:
        raise ValidationError("Manifest operation is not allowed in this phase")
    supplied_step_checksum = step.get("checksum", step.get("sha256"))
    if supplied_step_checksum is None:
        raise ValidationError("Manifest step checksum is required")
    checksum = _hex(supplied_step_checksum, field="step checksum")
    if _digest(_step_without_checksum(step)) != checksum:
        raise ValidationError("Manifest step checksum mismatch")
    payload = _operation_payload(step)
    table = _table_name(payload)
    normalized_payload: dict[str, Any]
    if operation == "create_table":
        _reject_unlisted_fields(payload, {"table", "table_name", "columns"})
        columns = payload.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValidationError("create_table requires non-empty columns")
        normalized_payload = {"table": table, "columns": [_column_spec(c) for c in columns]}
        if any(key in payload for key in ("unique_keys", "indexes", "constraints", "default", "check", "raw_sql")):
            raise ValidationError("create_table contains a forbidden field")
    elif operation == "add_column":
        _reject_unlisted_fields(payload, {"table", "table_name", "column", "name", "type", "required", "nullable"})
        normalized_payload = {"table": table, "column": _column_spec(payload)}
    elif operation == "add_index":
        _reject_unlisted_fields(payload, {"table", "table_name", "name", "columns"})
        columns = payload.get("columns")
        if not isinstance(columns, list) or not columns or any(not isinstance(c, str) for c in columns):
            raise ValidationError("add_index requires non-empty columns")
        if payload.get("unique") or payload.get("unique_key") or payload.get("constraint"):
            raise ValidationError("v1 indexes must be non-unique")
        if any(key in payload for key in ("expression", "where", "predicate", "include", "raw_sql")):
            raise ValidationError("v1 indexes only permit named columns")
        name = payload.get("name", f"idx_{table}_{'_'.join(columns)}")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValidationError("Manifest index name is invalid")
        normalized_payload = {"table": table, "name": name, "columns": [_column_name(c) for c in columns]}
    elif operation == "backfill_column":
        _reject_unlisted_fields(payload, {"table", "table_name", "column", "primary_key", "where_null", "batch_size", "value"})
        column = _column_name(payload.get("column"))
        if payload.get("where_null") is not True:
            raise ValidationError("backfill_column requires where_null=true")
        batch_size = payload.get("batch_size")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 1000:
            raise ValidationError("backfill_column batch_size must be between 1 and 1000")
        primary_key = _column_name(payload.get("primary_key", "id"))
        if primary_key != "id":
            raise ValidationError("backfill_column cursor must use the stable id primary key")
        if "expression" in payload or "where" in payload or "sql" in payload or "custom_code" in payload:
            raise ValidationError("backfill_column only permits a scalar value")
        value = payload.get("value")
        if isinstance(value, (dict, list, tuple, set)):
            raise ValidationError("backfill_column value must be scalar")
        normalized_payload = {
            "table": table,
            "column": column,
            "primary_key": primary_key,
            "batch_size": batch_size,
            "where_null": True,
            "value": value,
        }
    else:  # set_not_null
        _reject_unlisted_fields(payload, {"table", "table_name", "column"})
        raw_column = payload.get("column")
        if isinstance(raw_column, dict):
            raw_column = raw_column.get("name")
        normalized_payload = {"table": table, "column": _column_name(raw_column)}
    return {
        "id": step_id,
        "phase": phase,
        "operation": operation,
        "payload": normalized_payload,
        "checksum": checksum,
        "step_order": index,
    }


def validate_manifest(manifest: Any, manifest_checksum: str | None = None) -> dict[str, Any]:
    """Validate and normalize a v1 immutable release manifest.

    Returned data is safe for the worker and contains no caller-controlled
    arbitrary SQL or expressions.  Error messages intentionally omit values.
    """
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("Manifest must be an object") from exc
    if not isinstance(manifest, dict):
        raise ValidationError("Manifest must be an object")
    version = manifest.get("manifest_version", manifest.get("version"))
    if version != 1:
        raise ValidationError("Only manifest_version=1 is supported")
    unknown_top_level = set(manifest) - {
        "manifest_version",
        "steps",
        "expected_schema_fingerprint",
        "manifest_checksum",
        "checksum",
        "sha256",
    }
    if unknown_top_level:
        raise ValidationError("Manifest contains an unsupported top-level field")
    steps = manifest.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValidationError("Manifest steps must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    previous_phase = -1
    for index, raw in enumerate(steps):
        step = _normalize_step(raw, index)
        if step["id"] in ids:
            raise ValidationError("Manifest step ids must be unique")
        ids.add(step["id"])
        phase_order = _PHASES[step["phase"]]
        if phase_order < previous_phase:
            raise ValidationError("Manifest steps must be ordered by phase")
        previous_phase = phase_order
        normalized.append(step)
    # The checksum covers the canonical document exactly as published: only
    # checksum fields are removed.  The worker-normalized projection is not
    # substituted into the digest, so flat and payload-shaped contracts do
    # not accidentally hash to different meanings.
    canonical = _manifest_without_checksum(manifest)
    canonical["manifest_version"] = 1
    canonical["steps"] = [_step_without_checksum(step) for step in steps]
    # Preserve an expected fingerprint as a non-executable contract field.
    if "expected_schema_fingerprint" in manifest:
        expected = manifest["expected_schema_fingerprint"]
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9A-Fa-f]{8,256}", expected)):
            raise ValidationError("expected_schema_fingerprint is invalid")
        if expected is not None:
            canonical["expected_schema_fingerprint"] = expected
    computed = _digest(canonical)
    supplied = manifest_checksum or manifest.get("manifest_checksum", manifest.get("checksum"))
    if supplied is None:
        raise ValidationError("Manifest checksum is required")
    if _hex(supplied, field="manifest checksum") != computed:
        raise ValidationError("Manifest checksum mismatch")
    return {"manifest_version": 1, "steps": normalized, "checksum": computed, **({"expected_schema_fingerprint": canonical["expected_schema_fingerprint"]} if "expected_schema_fingerprint" in canonical else {})}


def manifest_checksum(manifest: Any) -> str:
    """Return the canonical v1 manifest checksum.

    A published manifest may carry its checksum field; callers constructing a
    release may omit that field and use this helper to calculate it.
    """
    if not isinstance(manifest, dict):
        raise ValidationError("Manifest must be an object")
    supplied = manifest.get("manifest_checksum", manifest.get("checksum", manifest.get("sha256")))
    if supplied is not None:
        return validate_manifest(manifest, supplied)["checksum"]
    steps = manifest.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValidationError("Manifest steps must be a non-empty array")
    for index, raw in enumerate(steps):
        _normalize_step(raw, index)
    canonical = _manifest_without_checksum(manifest)
    canonical["manifest_version"] = 1
    canonical["steps"] = [_step_without_checksum(step) for step in steps]
    return _digest(canonical)


def _reason(value: Any) -> str | None:
    if isinstance(value, str) and _REASON.fullmatch(value):
        return value
    return None


def _public_job(row: Any, targets: list[Any], steps: list[Any]) -> dict[str, Any]:
    by_target: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        by_target.setdefault(str(step["target_id"]), []).append(
            {
                "step_id": step["step_id"],
                "operation": step["operation"],
                "state": step["state"],
                "checkpoint": sanitize_checkpoint(step["checkpoint"]),
                "reason": _reason(step["reason_code"]),
            }
        )
    result = {
        "job_id": str(row["id"]),
        "app_id": str(row["app_id"]),
        "release_id": str(row["release_id"]),
        "manifest_checksum": row["manifest_checksum"],
        "snapshot_id": str(row["snapshot_id"]),
        "status": row["status"],
        "blocked_reason": _reason(row["blocked_reason"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        "targets": [
            {
                "target_id": str(target["id"]),
                "installation_id": str(target["installation_id"]),
                "vault_id": str(target["vault_id"]),
                "ordinal": target["ordinal"],
                "batch": target["batch_no"],
                "canary": target["is_canary"],
                "state": target["state"],
                "reason": _reason(target["reason_code"]),
                "steps": by_target.get(str(target["id"]), []),
            }
            for target in targets
        ],
    }
    if row.get("source_rollout_id") is not None:
        result["source_rollout_id"] = str(row["source_rollout_id"])
    return result


async def _load_public_job(conn: Any, app_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT id, app_id, release_id, manifest_checksum, snapshot_id, status, blocked_reason, created_at, updated_at, completed_at, source_rollout_id FROM app_rollout_jobs WHERE id=$1 AND app_id=$2",
        job_id,
        app_id,
    )
    if row is None:
        raise NotFoundError("Rollout", "not found")
    targets = await conn.fetch(
        "SELECT id, installation_id, vault_id, ordinal, batch_no, is_canary, state, reason_code FROM app_rollout_targets WHERE job_id=$1 ORDER BY ordinal",
        job_id,
    )
    steps = await conn.fetch(
        "SELECT target_id, step_id, operation, state, checkpoint, reason_code FROM app_rollout_steps WHERE job_id=$1 ORDER BY target_id, step_order",
        job_id,
    )
    return _public_job(row, targets, steps)


async def get_rollout(app_id: uuid.UUID | str, job_id: uuid.UUID | str) -> dict[str, Any]:
    app_id = _as_uuid(app_id, field="app_id")
    job_id = _as_uuid(job_id, field="rollout_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _load_public_job(conn, app_id, job_id)


async def request_rollout(
    app_id: uuid.UUID | str,
    *,
    release_id: uuid.UUID | str,
    manifest_checksum_value: str,
    idempotency_key: str,
    requested_by_kind: str,
    correlation_id: str,
    actor: str,
    actor_id: str,
) -> dict[str, Any]:
    app_id = _as_uuid(app_id, field="app_id")
    release_id = _as_uuid(release_id, field="release_id")
    key = _as_uuid(idempotency_key, field="Idempotency-Key")
    checksum = _hex(manifest_checksum_value, field="manifest checksum")
    if requested_by_kind not in {"admin", "app"}:
        raise ValidationError("requested_by_kind is invalid")
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"app-rollout:{app_id}")
                existing = await conn.fetchrow(
                    "SELECT id, release_id, manifest_checksum FROM app_rollout_jobs WHERE app_id=$1 AND idempotency_key=$2 FOR UPDATE",
                    app_id,
                    key,
                )
                if existing:
                    if existing["release_id"] != release_id or existing["manifest_checksum"] != checksum:
                        raise ConflictError("Idempotency-Key was already used with different rollout input")
                    result = await _load_public_job(conn, app_id, existing["id"])
                    result["replayed"] = True
                    return result
                release = await conn.fetchrow(
                    "SELECT id, manifest, manifest_checksum FROM app_releases WHERE app_id=$1 AND id=$2",
                    app_id,
                    release_id,
                )
                if release is None:
                    raise NotFoundError("Release", "not found")
                if release["manifest_checksum"] != checksum:
                    raise ConflictError("Release checksum does not match request")
                normalized = validate_manifest(release["manifest"], checksum)
                targets = await conn.fetch(
                    """
                    SELECT i.id AS installation_id, i.vault_id, i.current_release_id,
                           i.grant_generation, i.lifecycle,
                           g.status AS grant_status, g.generation AS active_generation,
                           o.observed_release_id, o.observed_grant_generation,
                           o.observed_generation
                      FROM vault_app_installations i
                      LEFT JOIN LATERAL (
                          SELECT status, generation FROM installation_grants
                           WHERE installation_id=i.id AND status='active'
                           ORDER BY generation DESC LIMIT 1
                      ) g ON TRUE
                      LEFT JOIN app_installation_observed_states o ON o.installation_id=i.id
                     WHERE i.app_id=$1 AND i.lifecycle='active'
                       AND i.current_release_id IS DISTINCT FROM $2
                     ORDER BY i.created_at, i.id
                    """,
                    app_id,
                    release_id,
                )
                if not targets:
                    raise ValidationError("No active installation requires this rollout")
                for target in targets:
                    if target["grant_status"] != "active" or target["active_generation"] != target["grant_generation"]:
                        raise ConflictError("Rollout preflight failed")
                    if target["observed_generation"] is None or target["observed_release_id"] != target["current_release_id"] or target["observed_grant_generation"] != target["grant_generation"]:
                        raise ConflictError("Rollout preflight failed")
                    for step in normalized["steps"]:
                        if step["operation"] == "create_table":
                            continue
                        table_name = step["payload"]["table"]
                        owned = await conn.fetchval(
                            "SELECT EXISTS (SELECT 1 FROM app_owned_resources WHERE installation_id=$1 AND vault_id=$2 AND resource_kind='table' AND resource_key=$3 AND status='owned')",
                            target["installation_id"],
                            target["vault_id"],
                            table_name,
                        )
                        if not owned:
                            raise ConflictError("Rollout preflight failed")
                snapshot = await conn.fetchrow(
                    "INSERT INTO app_rollout_snapshots(app_id, requested_by_kind) VALUES($1,$2) RETURNING id, created_at",
                    app_id,
                    requested_by_kind,
                )
                assert snapshot is not None
                for target in targets:
                    await conn.execute(
                        """INSERT INTO app_rollout_snapshot_targets(snapshot_id, app_id, installation_id, vault_id, desired_release_id, current_release_id, baseline_grant_generation, state) VALUES($1,$2,$3,$4,$5,$6,$7,'pending')""",
                        snapshot["id"], app_id, target["installation_id"], target["vault_id"], release_id, target["current_release_id"], target["grant_generation"],
                    )
                await conn.execute("UPDATE app_rollout_snapshots SET sealed_at=NOW() WHERE id=$1", snapshot["id"])
                job = await conn.fetchrow(
                    """INSERT INTO app_rollout_jobs(app_id, release_id, manifest_checksum, idempotency_key, snapshot_id, requested_by_kind) VALUES($1,$2,$3,$4,$5,$6) RETURNING id, created_at, updated_at, completed_at""",
                    app_id, release_id, checksum, key, snapshot["id"], requested_by_kind,
                )
                assert job is not None
                for ordinal, target in enumerate(targets):
                    target_row = await conn.fetchrow(
                        "SELECT id FROM app_rollout_snapshot_targets WHERE snapshot_id=$1 AND installation_id=$2",
                        snapshot["id"], target["installation_id"],
                    )
                    batch_no = 0 if ordinal == 0 else ((ordinal - 1) // 10) + 1
                    rollout_target = await conn.fetchrow(
                        """INSERT INTO app_rollout_targets(job_id, app_id, installation_id, snapshot_target_id, vault_id, release_id, ordinal, batch_no, is_canary) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                        job["id"], app_id, target["installation_id"], target_row["id"], target["vault_id"], release_id, ordinal, batch_no, ordinal == 0,
                    )
                    assert rollout_target is not None
                    for step in normalized["steps"]:
                        await conn.execute(
                            """INSERT INTO app_rollout_steps(job_id, target_id, installation_id, release_id, step_id, step_order, step_checksum, operation) VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
                            job["id"], rollout_target["id"], target["installation_id"], release_id, step["id"], step["step_order"], step["checksum"], step["operation"],
                        )
                    await conn.execute(
                        "UPDATE vault_app_installations SET desired_release_id=$2, lifecycle='upgrading', blocked_reason=NULL WHERE id=$1 AND app_id=$3",
                        target["installation_id"], release_id, app_id,
                    )
                await conn.execute(
                    "INSERT INTO app_rollout_audit(job_id,app_id,action,outcome,reason_code) VALUES($1,$2,'request','accepted','accepted')",
                    job["id"],
                    app_id,
                )
                result = await _load_public_job(conn, app_id, job["id"])
    except (ConflictError, ValidationError, NotFoundError):
        record_app_audit("app.rollout.request", correlation_id=correlation_id, outcome="error", reason="rejected", actor=actor, actor_id=actor_id, app_id=app_id)
        raise
    record_app_audit("app.rollout.request", correlation_id=correlation_id, outcome="ok", reason="accepted", actor=actor, actor_id=actor_id, app_id=app_id)
    result["replayed"] = False
    return result


async def request_rollout_as_admin(
    app_id: uuid.UUID | str,
    *,
    release_id: uuid.UUID | str,
    manifest_checksum_value: str,
    idempotency_key: str,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    if not user.is_admin:
        raise ForbiddenError("System administrator permission required")
    return await request_rollout(
        app_id,
        release_id=release_id,
        manifest_checksum_value=manifest_checksum_value,
        idempotency_key=idempotency_key,
        requested_by_kind="admin",
        correlation_id=correlation_id,
        actor=user.username,
        actor_id=user.user_id,
    )


async def request_rollout_as_app(
    principal: AppPrincipal,
    *,
    release_id: uuid.UUID | str,
    manifest_checksum_value: str,
    idempotency_key: str,
    correlation_id: str,
) -> dict[str, Any]:
    await authorize_app_capability(principal, capability="rollout:request", correlation_id=correlation_id)
    return await request_rollout(
        principal.app_id,
        release_id=release_id,
        manifest_checksum_value=manifest_checksum_value,
        idempotency_key=idempotency_key,
        requested_by_kind="app",
        correlation_id=correlation_id,
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
    )


async def get_rollout_as_app(principal: AppPrincipal, job_id: uuid.UUID | str, *, correlation_id: str) -> dict[str, Any]:
    await authorize_app_capability(principal, capability="rollout:read", correlation_id=correlation_id)
    return await get_rollout(principal.app_id, job_id)


async def resume_rollout(
    app_id: uuid.UUID | str,
    source_rollout_id: uuid.UUID | str,
    *,
    release_id: uuid.UUID | str,
    manifest_checksum_value: str,
    idempotency_key: str,
    requested_by_kind: str,
    correlation_id: str,
    actor: str,
    actor_id: str,
) -> dict[str, Any]:
    """Create a new rollout attempt from a blocked immutable source job.

    The source job, snapshot, targets, steps, and checkpoints are read only.
    Only the new job and its freshly sealed snapshot are mutated.  This keeps
    recovery auditable and prevents a retry from rewriting the failed attempt.
    """
    app_uuid = _as_uuid(app_id, field="app_id")
    source_uuid = _as_uuid(source_rollout_id, field="rollout_id")
    release_uuid = _as_uuid(release_id, field="release_id")
    key = _as_uuid(idempotency_key, field="Idempotency-Key")
    checksum = _hex(manifest_checksum_value, field="manifest checksum")
    if requested_by_kind not in {"admin", "app"}:
        raise ValidationError("requested_by_kind is invalid")
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"app-rollout-resume:{source_uuid}",
                )
                existing = await conn.fetchrow(
                    """
                    SELECT new_rollout_id, release_id, manifest_checksum
                      FROM app_rollout_resume_attempts
                     WHERE source_rollout_id=$1 AND idempotency_key=$2
                     FOR UPDATE
                    """,
                    source_uuid,
                    key,
                )
                if existing is not None:
                    if (
                        existing["release_id"] != release_uuid
                        or existing["manifest_checksum"] != checksum
                    ):
                        raise ConflictError("Idempotency-Key was already used with different resume input")
                    result = await _load_public_job(conn, app_uuid, existing["new_rollout_id"])
                    await conn.execute(
                        """
                        INSERT INTO app_rollout_audit(
                            job_id, app_id, action, outcome, reason_code
                        ) VALUES($1,$2,'resume','replayed','same_attempt')
                        """,
                        existing["new_rollout_id"],
                        app_uuid,
                    )
                    result.update(
                        {
                            "replayed": True,
                            "resume_outcome": "replayed",
                            "resume_reason": "same_attempt",
                            "source_rollout_id": str(source_uuid),
                        }
                    )
                    record_app_audit(
                        "app.rollout.resume",
                        correlation_id=correlation_id,
                        outcome="ok",
                        reason="replayed",
                        actor=actor,
                        actor_id=actor_id,
                        app_id=app_uuid,
                    )
                    return result

                source = await conn.fetchrow(
                    """
                    SELECT id, app_id, release_id, manifest_checksum, status
                      FROM app_rollout_jobs
                     WHERE id=$1 AND app_id=$2
                     FOR SHARE
                    """,
                    source_uuid,
                    app_uuid,
                )
                if source is None:
                    raise NotFoundError("Rollout", "not found")
                if source["status"] != "blocked":
                    raise ConflictError("Only a blocked rollout can be resumed")
                if source["release_id"] != release_uuid:
                    raise ConflictError("Resume release does not match the source rollout")
                if source["manifest_checksum"] != checksum:
                    raise ConflictError("Resume checksum does not match the source rollout")
                release = await conn.fetchrow(
                    "SELECT id, manifest, manifest_checksum FROM app_releases WHERE app_id=$1 AND id=$2",
                    app_uuid,
                    release_uuid,
                )
                if release is None:
                    raise NotFoundError("Release", "not found")
                if release["manifest_checksum"] != checksum:
                    raise ConflictError("Release checksum does not match request")
                normalized = validate_manifest(release["manifest"], checksum)
                source_targets = await conn.fetch(
                    """
                    SELECT installation_id, ordinal
                      FROM app_rollout_targets
                     WHERE job_id=$1
                     ORDER BY ordinal, installation_id
                    """,
                    source_uuid,
                )
                if not source_targets:
                    raise ConflictError("Source rollout has no targets")

                installations = await conn.fetch(
                    """
                    SELECT i.id AS installation_id, i.vault_id, i.current_release_id,
                           i.desired_release_id, i.grant_generation, i.lifecycle,
                           g.generation AS active_generation, g.status AS grant_status,
                           o.observed_release_id, o.observed_grant_generation,
                           o.observed_generation, st.ordinal
                      FROM vault_app_installations AS i
                      JOIN app_rollout_targets AS st
                        ON st.job_id=$3 AND st.installation_id=i.id
                      LEFT JOIN LATERAL (
                          SELECT generation, status
                            FROM installation_grants
                           WHERE installation_id=i.id AND status='active'
                           ORDER BY generation DESC LIMIT 1
                      ) AS g ON TRUE
                      LEFT JOIN app_installation_observed_states AS o
                        ON o.installation_id=i.id
                     WHERE i.app_id=$1
                       AND (
                           (i.lifecycle='blocked' AND i.desired_release_id=$2)
                           OR i.lifecycle='active'
                       )
                     ORDER BY st.ordinal, i.id
                     FOR UPDATE OF i
                    """,
                    app_uuid,
                    release_uuid,
                    source_uuid,
                )
                if (
                    len(installations) != len(source_targets)
                    or {target["installation_id"] for target in installations}
                    != {target["installation_id"] for target in source_targets}
                ):
                    raise ConflictError("Resume target set changed")

                # Validate every candidate before creating the snapshot/job so
                # a stale grant or observation cannot leave partial recovery.
                for target in installations:
                    if target["grant_status"] != "active" or target["active_generation"] != target["grant_generation"]:
                        raise ConflictError("Resume preflight failed")
                    if target["observed_generation"] is None or target["observed_release_id"] != target["current_release_id"] or target["observed_grant_generation"] != target["grant_generation"]:
                        raise ConflictError("Resume preflight failed")
                    for step in normalized["steps"]:
                        if step["operation"] == "create_table":
                            continue
                        owned = await conn.fetchval(
                            """
                            SELECT EXISTS (
                                SELECT 1 FROM app_owned_resources
                                 WHERE installation_id=$1 AND vault_id=$2
                                   AND resource_kind='table' AND resource_key=$3
                                   AND status='owned'
                            )
                            """,
                            target["installation_id"],
                            target["vault_id"],
                            step["payload"]["table"],
                        )
                        if not owned:
                            raise ConflictError("Resume preflight failed")

                snapshot = await conn.fetchrow(
                    "INSERT INTO app_rollout_snapshots(app_id, requested_by_kind) VALUES($1,$2) RETURNING id",
                    app_uuid,
                    requested_by_kind,
                )
                assert snapshot is not None
                job_id = uuid.uuid4()
                for target in installations:
                    converged = target["current_release_id"] == release_uuid
                    snapshot_target = await conn.fetchrow(
                        """
                        INSERT INTO app_rollout_snapshot_targets(
                            snapshot_id, app_id, installation_id, vault_id,
                            desired_release_id, current_release_id,
                            baseline_grant_generation, state, reason_code
                        ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id
                        """,
                        snapshot["id"],
                        app_uuid,
                        target["installation_id"],
                        target["vault_id"],
                        release_uuid,
                        target["current_release_id"],
                        target["grant_generation"],
                        "replayed" if converged else "pending",
                        "already_converged" if converged else None,
                    )
                    assert snapshot_target is not None

                await conn.execute(
                    "UPDATE app_rollout_snapshots SET sealed_at=NOW() WHERE id=$1",
                    snapshot["id"],
                )
                pending = [
                    target for target in installations
                    if target["current_release_id"] != release_uuid
                ]
                job_status = "applied" if not pending else "pending"
                job = await conn.fetchrow(
                    """
                    INSERT INTO app_rollout_jobs(
                        id, app_id, release_id, manifest_checksum,
                        idempotency_key, snapshot_id, source_rollout_id,
                        requested_by_kind, status, completed_at
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,
                              CASE WHEN $9='applied' THEN NOW() ELSE NULL END)
                    RETURNING id, created_at, updated_at, completed_at
                    """,
                    job_id,
                    app_uuid,
                    release_uuid,
                    checksum,
                    key,
                    snapshot["id"],
                    source_uuid,
                    requested_by_kind,
                    job_status,
                )
                assert job is not None
                for ordinal, target in enumerate(installations):
                    snapshot_target_id = await conn.fetchval(
                        "SELECT id FROM app_rollout_snapshot_targets WHERE snapshot_id=$1 AND installation_id=$2",
                        snapshot["id"],
                        target["installation_id"],
                    )
                    converged = target["current_release_id"] == release_uuid
                    batch_no = 0 if ordinal == 0 else ((ordinal - 1) // 10) + 1
                    rollout_target = await conn.fetchrow(
                        """
                        INSERT INTO app_rollout_targets(
                            job_id, app_id, installation_id, snapshot_target_id,
                            vault_id, release_id, ordinal, batch_no, is_canary, state
                        ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id
                        """,
                        job_id,
                        app_uuid,
                        target["installation_id"],
                        snapshot_target_id,
                        target["vault_id"],
                        release_uuid,
                        ordinal,
                        batch_no,
                        ordinal == 0,
                        "replayed" if converged else "pending",
                    )
                    assert rollout_target is not None
                    if converged:
                        continue
                    for step in normalized["steps"]:
                        await conn.execute(
                            """
                            INSERT INTO app_rollout_steps(
                                job_id, target_id, installation_id, release_id,
                                step_id, step_order, step_checksum, operation
                            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                            """,
                            job_id,
                            rollout_target["id"],
                            target["installation_id"],
                            release_uuid,
                            step["id"],
                            step["step_order"],
                            step["checksum"],
                            step["operation"],
                        )
                    await conn.execute(
                        """
                        UPDATE vault_app_installations
                           SET desired_release_id=$2, lifecycle='upgrading', blocked_reason=NULL
                         WHERE id=$1 AND app_id=$3
                        """,
                        target["installation_id"],
                        release_uuid,
                        app_uuid,
                    )
                await conn.execute(
                    """
                    INSERT INTO app_rollout_audit(
                        job_id, app_id, action, outcome, reason_code
                    ) VALUES($1,$2,'resume','accepted','new_attempt')
                    """,
                    job_id,
                    app_uuid,
                )
                await conn.execute(
                    """
                    INSERT INTO app_rollout_resume_attempts(
                        app_id, source_rollout_id, new_rollout_id, idempotency_key,
                        release_id, manifest_checksum, requested_by_kind, outcome
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,'accepted')
                    """,
                    app_uuid,
                    source_uuid,
                    job_id,
                    key,
                    release_uuid,
                    checksum,
                    requested_by_kind,
                )
                result = await _load_public_job(conn, app_uuid, job_id)
                result.update(
                    {
                        "replayed": False,
                        "resume_outcome": "accepted",
                        "resume_reason": "new_attempt",
                        "source_rollout_id": str(source_uuid),
                    }
                )
    except asyncpg.UniqueViolationError:
        raise ConflictError("Resume request conflicted with another attempt") from None
    except (ConflictError, ValidationError, NotFoundError):
        record_app_audit(
            "app.rollout.resume",
            correlation_id=correlation_id,
            outcome="error",
            reason="rejected",
            actor=actor,
            actor_id=actor_id,
            app_id=app_uuid,
        )
        raise
    record_app_audit(
        "app.rollout.resume",
        correlation_id=correlation_id,
        outcome="ok",
        reason=result.get("resume_outcome", "accepted"),
        actor=actor,
        actor_id=actor_id,
        app_id=app_uuid,
    )
    return result


async def resume_rollout_as_admin(
    app_id: uuid.UUID | str,
    source_rollout_id: uuid.UUID | str,
    *,
    release_id: uuid.UUID | str,
    manifest_checksum_value: str,
    idempotency_key: str,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    if not user.is_admin:
        raise ForbiddenError("System administrator permission required")
    return await resume_rollout(
        app_id,
        source_rollout_id,
        release_id=release_id,
        manifest_checksum_value=manifest_checksum_value,
        idempotency_key=idempotency_key,
        requested_by_kind="admin",
        correlation_id=correlation_id,
        actor=user.username,
        actor_id=user.user_id,
    )


async def resume_rollout_as_app(
    principal: AppPrincipal,
    source_rollout_id: uuid.UUID | str,
    *,
    release_id: uuid.UUID | str,
    manifest_checksum_value: str,
    idempotency_key: str,
    correlation_id: str,
) -> dict[str, Any]:
    await authorize_app_capability(principal, capability="rollout:request", correlation_id=correlation_id)
    return await resume_rollout(
        principal.app_id,
        source_rollout_id,
        release_id=release_id,
        manifest_checksum_value=manifest_checksum_value,
        idempotency_key=idempotency_key,
        requested_by_kind="app",
        correlation_id=correlation_id,
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
    )
