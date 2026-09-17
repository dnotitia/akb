"""Metadata-only adoption of operator-declared legacy app tables."""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.services.access_service import check_vault_access
from app.services.app_identity_service import record_app_audit
from app.services.app_inventory_service import expected_schema_fingerprint
from app.services.app_rollout_service import validate_manifest
from app.services.app_resource_service import (
    canonical_json,
    canonical_table_fingerprint,
    fetch_allowlisted_tables,
    lock_app_vault_pair,
    lock_table_mutation,
    normalize_table_allowlist,
    table_ownership,
)
from app.services.auth_service import AuthenticatedUser
from app.util.text import to_nfc_any

_REASONS = {
    "missing_table",
    "ownership_conflict",
    "installation_exists",
    "release_fingerprint_missing",
    "fingerprint_mismatch",
    "fingerprint_changed",
    "created",
    "replayed",
    "applied",
    "adopted",
    "vault_missing",
    "apply_conflict",
}


def _as_uuid(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"{field} must be a UUID") from exc


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


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _safe_reason(value: str | None) -> str | None:
    return value if value in _REASONS else None


def normalize_adoption_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize target identity before authorization or lookup."""

    if not isinstance(targets, list) or not targets:
        raise ValidationError("targets must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    seen_vaults: set[uuid.UUID] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValidationError("each adoption target must be an object")
        if set(target) - {"vault_id", "table_allowlist"}:
            raise ValidationError("adoption target contains an unsupported field")
        raw_vault_id = target.get("vault_id")
        if not isinstance(raw_vault_id, (str, uuid.UUID)):
            raise ValidationError("vault_id must be a UUID")
        vault_id = _as_uuid(raw_vault_id, field="vault_id")
        if vault_id in seen_vaults:
            raise ValidationError("targets must contain each Vault only once")
        seen_vaults.add(vault_id)
        raw_tables = target.get("table_allowlist")
        if not isinstance(raw_tables, list):
            raise ValidationError("table allowlist must be an array")
        tables = normalize_table_allowlist(cast(list[str], raw_tables))
        normalized.append(
            {
                "vault_id": str(vault_id),
                "table_allowlist": tables,
            }
        )
    normalized.sort(key=lambda item: item["vault_id"])
    return normalized


def adoption_input_digest(
    app_id: uuid.UUID | str,
    baseline_release_id: uuid.UUID | str,
    targets: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    payload = {
        "app_id": str(_as_uuid(app_id, field="app_id")),
        "baseline_release_id": str(
            _as_uuid(baseline_release_id, field="baseline_release_id")
        ),
        "targets": to_nfc_any(targets),
    }
    import hashlib

    return payload, hashlib.sha256(canonical_json(payload)).hexdigest()


async def _authorize_target_vaults(
    user: AuthenticatedUser,
    targets: list[dict[str, Any]],
    *,
    app_id: uuid.UUID,
    action: str,
    correlation_id: str,
) -> str:
    """Authorize every target before resolving app/release/table metadata."""

    if user.is_admin:
        return "system_admin"
    pool = await get_pool()
    target_ids = [uuid.UUID(item["vault_id"]) for item in targets]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name FROM vaults WHERE id = ANY($1::uuid[])", target_ids
        )
    by_id = {row["id"]: row["name"] for row in rows}
    if len(by_id) != len(target_ids):
        record_app_audit(
            action,
            correlation_id=correlation_id,
            outcome="error",
            reason="vault_admin_required",
            actor=user.username,
            actor_id=user.user_id,
        )
        raise ForbiddenError("Legacy adoption request denied")
    try:
        for vault_id in target_ids:
            await check_vault_access(user.user_id, by_id[vault_id], required_role="admin")
    except (ForbiddenError, NotFoundError):
        record_app_audit(
            action,
            correlation_id=correlation_id,
            outcome="error",
            reason="vault_admin_required",
            actor=user.username,
            actor_id=user.user_id,
            app_id=app_id,
        )
        raise ForbiddenError("Legacy adoption request denied") from None
    return "vault_admin"


async def _authorize_existing_plan(
    user: AuthenticatedUser,
    app_id: uuid.UUID,
    adoption_id: uuid.UUID,
    *,
    action: str,
    correlation_id: str,
) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        target_rows = await conn.fetch(
            """
            SELECT vault_id
              FROM app_legacy_adoption_targets
             WHERE adoption_id = $1 AND app_id = $2
             ORDER BY target_order
            """,
            adoption_id,
            app_id,
        )
        if not target_rows:
            raise NotFoundError("Legacy adoption", "not found")
        vault_rows = await conn.fetch(
            "SELECT id, name FROM vaults WHERE id = ANY($1::uuid[])",
            [row["vault_id"] for row in target_rows],
        )
    if user.is_admin:
        return "system_admin"
    names = {row["id"]: row["name"] for row in vault_rows}
    try:
        if len(names) != len(target_rows):
            raise NotFoundError("Legacy adoption", "not found")
        for row in target_rows:
            await check_vault_access(user.user_id, names[row["vault_id"]], required_role="admin")
    except (ForbiddenError, NotFoundError):
        record_app_audit(
            action,
            correlation_id=correlation_id,
            outcome="error",
            reason="vault_admin_required",
            actor=user.username,
            actor_id=user.user_id,
            app_id=app_id,
        )
        # The read/apply surface intentionally uses the same response for a
        # foreign/random adoption id and an unauthorized existing one.
        raise NotFoundError("Legacy adoption", "not found") from None
    return "vault_admin"


async def _fetch_preflight(
    conn: Any,
    *,
    app_id: uuid.UUID,
    release: Any,
    target: dict[str, Any],
    lock_tables: bool = False,
) -> dict[str, Any]:
    vault_id = uuid.UUID(target["vault_id"])
    table_allowlist = list(target["table_allowlist"])
    if await conn.fetchval("SELECT 1 FROM vaults WHERE id = $1", vault_id) is None:
        raise NotFoundError("Vault", "not found")
    tables = await fetch_allowlisted_tables(
        conn,
        vault_id,
        table_allowlist,
        lock=lock_tables,
    )
    found_names = {row["name"] for row in tables}
    missing = sorted(set(table_allowlist) - found_names)
    actual_fingerprint = canonical_table_fingerprint(tables)
    ownership_conflicts: list[str] = []
    ownership_conflict_installations: list[uuid.UUID] = []
    for table_name in sorted(found_names):
        ownership = await table_ownership(conn, vault_id, table_name)
        if ownership is not None:
            ownership_conflicts.append(table_name)
            installation = ownership.get("installation_id")
            if isinstance(installation, uuid.UUID):
                ownership_conflict_installations.append(installation)
    installation_id = await conn.fetchval(
        "SELECT id FROM vault_app_installations WHERE app_id = $1 AND vault_id = $2",
        app_id,
        vault_id,
    )
    # v2 derives the expected baseline from the complete desired projection;
    # the operator does not supply a second, independently trusted checksum.
    release_expected = expected_schema_fingerprint(release["manifest"])
    reason: str | None = None
    if missing:
        reason = "missing_table"
    elif ownership_conflicts:
        reason = "ownership_conflict"
    elif installation_id is not None:
        reason = "installation_exists"
    elif release_expected is None:
        reason = "release_fingerprint_missing"
    elif actual_fingerprint != release_expected:
        reason = "fingerprint_mismatch"

    planned_metadata = {
        "installation": {
            "lifecycle": "active",
            "desired_release_id": str(release["id"]),
            "current_release_id": str(release["id"]),
            "grant_generation": 0,
        },
        "owned_resources": [
            {"kind": "table", "key": name} for name in sorted(found_names)
        ],
        "observed": {
            "release_id": str(release["id"]),
            "schema_fingerprint": actual_fingerprint,
            "grant_generation": 0,
        },
    }
    return {
        "expected_schema_fingerprint": release_expected,
        "actual_schema_fingerprint": actual_fingerprint,
        "included_tables": sorted(found_names),
        "excluded_tables": [],
        "missing_tables": missing,
        "ownership_conflicts": ownership_conflicts,
        "ownership_conflict_installations": ownership_conflict_installations,
        "reason_code": reason,
        "state": "planned" if reason is None else "blocked",
        "installation_id": installation_id,
        "checkpoint": {
            "phase": "preflight",
            "code": "ready" if reason is None else reason,
        },
        "planned_metadata": planned_metadata,
    }


def _target_projection(row: Any, *, replayed: bool | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target_id": str(row["id"]),
        "vault_id": str(row["vault_id"]),
        "table_allowlist": _json_list(row["table_allowlist"]),
        "included_tables": _json_list(row["included_tables"]),
        "excluded_tables": _json_list(row["excluded_tables"]),
        "missing_tables": _json_list(row["missing_tables"]),
        "ownership_conflicts": _json_list(row["ownership_conflicts"]),
        "expected_schema_fingerprint": row["expected_schema_fingerprint"],
        "actual_schema_fingerprint": row["actual_schema_fingerprint"],
        "state": row["state"],
        "reason_code": _safe_reason(row["reason_code"]),
        "installation_id": (
            str(row["installation_id"]) if row["installation_id"] is not None else None
        ),
        "checkpoint": _json_object(row["checkpoint"]),
        "planned_metadata": _json_object(row["planned_metadata"]),
    }
    if replayed is not None:
        result["replayed"] = replayed
        if replayed:
            result["outcome"] = "replayed"
    return result


async def _load_projection(conn: Any, app_id: uuid.UUID, adoption_id: uuid.UUID) -> dict[str, Any]:
    plan = await conn.fetchrow(
        "SELECT * FROM app_legacy_adoption_plans WHERE id = $1 AND app_id = $2",
        adoption_id,
        app_id,
    )
    if plan is None:
        raise NotFoundError("Legacy adoption", "not found")
    targets = await conn.fetch(
        """
        SELECT *
          FROM app_legacy_adoption_targets
         WHERE adoption_id = $1
         ORDER BY target_order
        """,
        adoption_id,
    )
    return {
        "adoption_id": str(plan["id"]),
        "app_id": str(plan["app_id"]),
        "baseline_release_id": str(plan["baseline_release_id"]),
        "idempotency_key": str(plan["idempotency_key"]),
        "input_digest": plan["input_digest"],
        "status": plan["status"],
        "targets": [_target_projection(row) for row in targets],
        "checkpoint": {
            "target_count": len(targets),
            "applied_count": sum(row["state"] in {"applied", "replayed"} for row in targets),
            "blocked_count": sum(row["state"] == "blocked" for row in targets),
        },
        "created_at": plan["created_at"],
        "updated_at": plan["updated_at"],
        "applied_at": plan["applied_at"],
    }


async def _record_ledger_audit(
    conn: Any,
    *,
    app_id: uuid.UUID,
    adoption_id: uuid.UUID,
    target_id: uuid.UUID | None,
    installation_id: uuid.UUID | None,
    vault_id: uuid.UUID | None,
    release_id: uuid.UUID,
    action: str,
    outcome: str,
    reason_code: str,
    correlation_id: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO app_legacy_adoption_audit
            (app_id, adoption_id, target_id, installation_id, vault_id,
             release_id, action, outcome, reason_code, correlation_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """,
        app_id,
        adoption_id,
        target_id,
        installation_id,
        vault_id,
        release_id,
        action,
        outcome,
        reason_code,
        correlation_id,
    )


async def _refresh_plan_status(conn: Any, adoption_id: uuid.UUID) -> None:
    states = await conn.fetch(
        "SELECT state FROM app_legacy_adoption_targets WHERE adoption_id = $1",
        adoption_id,
    )
    values = [row["state"] for row in states]
    if values and all(value in {"applied", "replayed"} for value in values):
        status = "applied"
        applied_at = "NOW()"
    elif any(value in {"applied", "replayed"} for value in values):
        status = "partial"
        applied_at = "NULL"
    elif values and all(value == "blocked" for value in values):
        status = "blocked"
        applied_at = "NULL"
    else:
        status = "planned"
        applied_at = "NULL"
    await conn.execute(
        f"UPDATE app_legacy_adoption_plans SET status=$2, applied_at={applied_at} WHERE id=$1",
        adoption_id,
        status,
    )


async def create_legacy_adoption(
    app_id: uuid.UUID | str,
    *,
    baseline_release_id: uuid.UUID | str,
    idempotency_key: uuid.UUID | str,
    targets: list[dict[str, Any]],
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    app_uuid = _as_uuid(app_id, field="app_id")
    release_uuid = _as_uuid(baseline_release_id, field="baseline_release_id")
    key = _as_uuid(idempotency_key, field="Idempotency-Key")
    normalized_targets = normalize_adoption_targets(targets)
    input_payload, digest = adoption_input_digest(app_uuid, release_uuid, normalized_targets)
    actor_kind = await _authorize_target_vaults(
        user,
        normalized_targets,
        app_id=app_uuid,
        action="app.legacy_adoption.create",
        correlation_id=correlation_id,
    )

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"legacy-adoption:{app_uuid}:{key}",
                )
                existing = await conn.fetchrow(
                    """
                    SELECT id, input_digest
                      FROM app_legacy_adoption_plans
                     WHERE app_id = $1 AND idempotency_key = $2
                     FOR UPDATE
                    """,
                    app_uuid,
                    key,
                )
                if existing is not None:
                    if existing["input_digest"] != digest:
                        raise ConflictError("Idempotency-Key was already used with different adoption input")
                    await _record_ledger_audit(
                        conn,
                        app_id=app_uuid,
                        adoption_id=existing["id"],
                        target_id=None,
                        installation_id=None,
                        vault_id=None,
                        release_id=release_uuid,
                        action="plan_replayed",
                        outcome="replay",
                        reason_code="replayed",
                        correlation_id=correlation_id,
                    )
                    result = await _load_projection(conn, app_uuid, existing["id"])
                    result["replayed"] = True
                else:
                    app = await conn.fetchrow(
                        "SELECT id, app_key FROM app_definitions WHERE id = $1", app_uuid
                    )
                    release = await conn.fetchrow(
                        """
                        SELECT id, app_id, version, manifest, manifest_checksum
                          FROM app_releases
                         WHERE app_id = $1 AND id = $2
                        """,
                        app_uuid,
                        release_uuid,
                    )
                    if app is None or release is None:
                        raise NotFoundError("App or release", "not found")
                    validated_manifest = validate_manifest(
                        release["manifest"],
                        release["manifest_checksum"],
                        version=release["version"],
                    )
                    if validated_manifest["app_key"] != app["app_key"]:
                        raise ConflictError(
                            "Release manifest app_key does not match the app definition"
                        )
                    plan = await conn.fetchrow(
                        """
                        INSERT INTO app_legacy_adoption_plans
                            (app_id, baseline_release_id, idempotency_key,
                             input_digest, input, requested_by)
                        VALUES ($1,$2,$3,$4,$5::jsonb,$6)
                        RETURNING id
                        """,
                        app_uuid,
                        release_uuid,
                        key,
                        digest,
                        json.dumps(input_payload, ensure_ascii=False, separators=(",", ":")),
                        user.username,
                    )
                    assert plan is not None
                    for index, target in enumerate(normalized_targets):
                        preflight = await _fetch_preflight(
                            conn,
                            app_id=app_uuid,
                            release=release,
                            target=target,
                        )
                        await conn.execute(
                            """
                            INSERT INTO app_legacy_adoption_targets
                                (adoption_id, app_id, vault_id, target_order,
                                 table_allowlist, expected_schema_fingerprint,
                                 actual_schema_fingerprint, included_tables,
                                 excluded_tables, missing_tables,
                                 ownership_conflicts, state, reason_code,
                                 installation_id, checkpoint, planned_metadata)
                            VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8::jsonb,$9::jsonb,
                                    $10::jsonb,$11::jsonb,$12,$13,$14,$15::jsonb,$16::jsonb)
                            """,
                            plan["id"],
                            app_uuid,
                            uuid.UUID(target["vault_id"]),
                            index,
                            json.dumps(target["table_allowlist"], separators=(",", ":")),
                            preflight["expected_schema_fingerprint"],
                            preflight["actual_schema_fingerprint"],
                            json.dumps(preflight["included_tables"], separators=(",", ":")),
                            json.dumps(preflight["excluded_tables"], separators=(",", ":")),
                            json.dumps(preflight["missing_tables"], separators=(",", ":")),
                            json.dumps(preflight["ownership_conflicts"], separators=(",", ":")),
                            preflight["state"],
                            preflight["reason_code"],
                            preflight["installation_id"],
                            json.dumps(preflight["checkpoint"], separators=(",", ":")),
                            json.dumps(preflight["planned_metadata"], separators=(",", ":")),
                        )
                    await _refresh_plan_status(conn, plan["id"])
                    await _record_ledger_audit(
                        conn,
                        app_id=app_uuid,
                        adoption_id=plan["id"],
                        target_id=None,
                        installation_id=None,
                        vault_id=None,
                        release_id=release_uuid,
                        action="plan_created",
                        outcome="ok",
                        reason_code="created",
                        correlation_id=correlation_id,
                    )
                    result = await _load_projection(conn, app_uuid, plan["id"])
                    result["replayed"] = False
    except asyncpg.UniqueViolationError:
        raise ConflictError("Legacy adoption request conflicted with another request") from None
    except (ConflictError, NotFoundError, ValidationError):
        record_app_audit(
            "app.legacy_adoption.create",
            correlation_id=correlation_id,
            outcome="error",
            reason="rejected",
            actor=user.username,
            actor_id=user.user_id,
            app_id=app_uuid,
        )
        raise

    record_app_audit(
        "app.legacy_adoption.create",
        correlation_id=correlation_id,
        outcome="replay" if result.get("replayed") else "ok",
        reason="replayed" if result.get("replayed") else "created",
        actor=actor_kind,
        actor_id=user.user_id,
        app_id=app_uuid,
    )
    return result


async def get_legacy_adoption(
    app_id: uuid.UUID | str,
    adoption_id: uuid.UUID | str,
    *,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    app_uuid = _as_uuid(app_id, field="app_id")
    adoption_uuid = _as_uuid(adoption_id, field="adoption_id")
    actor_kind = await _authorize_existing_plan(
        user,
        app_uuid,
        adoption_uuid,
        action="app.legacy_adoption.read",
        correlation_id=correlation_id,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await _load_projection(conn, app_uuid, adoption_uuid)
    record_app_audit(
        "app.legacy_adoption.read",
        correlation_id=correlation_id,
        outcome="ok",
        reason="read",
        actor=actor_kind,
        actor_id=user.user_id,
        app_id=app_uuid,
    )
    return result


async def _apply_target(
    app_id: uuid.UUID,
    adoption_id: uuid.UUID,
    target_id: uuid.UUID,
    *,
    correlation_id: str,
) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                plan = await conn.fetchrow(
                    "SELECT * FROM app_legacy_adoption_plans WHERE id=$1 AND app_id=$2 FOR UPDATE",
                    adoption_id,
                    app_id,
                )
                target_row = await conn.fetchrow(
                    "SELECT * FROM app_legacy_adoption_targets WHERE id=$1 AND adoption_id=$2 AND app_id=$3 FOR UPDATE",
                    target_id,
                    adoption_id,
                    app_id,
                )
                if plan is None or target_row is None:
                    raise NotFoundError("Legacy adoption", "not found")
                if target_row["state"] in {"applied", "replayed"}:
                    await conn.execute(
                        "UPDATE app_legacy_adoption_targets SET state='replayed' WHERE id=$1",
                        target_id,
                    )
                    await _record_ledger_audit(
                        conn,
                        app_id=app_id,
                        adoption_id=adoption_id,
                        target_id=target_id,
                        installation_id=target_row["installation_id"],
                        vault_id=target_row["vault_id"],
                        release_id=plan["baseline_release_id"],
                        action="target_replayed",
                        outcome="replay",
                        reason_code="replayed",
                        correlation_id=correlation_id,
                    )
                    await _refresh_plan_status(conn, adoption_id)
                    return

                await lock_app_vault_pair(conn, app_id, target_row["vault_id"])
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"legacy-adoption-resource:{target_row['vault_id']}:{','.join(_json_list(target_row['table_allowlist']))}",
                )
                for table_name in _json_list(target_row["table_allowlist"]):
                    await lock_table_mutation(conn, target_row["vault_id"], table_name)
                release = await conn.fetchrow(
                    "SELECT id, app_id, version, manifest FROM app_releases WHERE app_id=$1 AND id=$2",
                    app_id,
                    plan["baseline_release_id"],
                )
                if release is None:
                    raise NotFoundError("App or release", "not found")
                target = {
                    "vault_id": str(target_row["vault_id"]),
                    "table_allowlist": _json_list(target_row["table_allowlist"]),
                }
                preflight = await _fetch_preflight(
                    conn,
                    app_id=app_id,
                    release=release,
                    target=target,
                    lock_tables=True,
                )
                if (
                    preflight["reason_code"] == "fingerprint_mismatch"
                    and target_row["state"] == "planned"
                    and target_row["actual_schema_fingerprint"]
                    == target_row["expected_schema_fingerprint"]
                ):
                    preflight["reason_code"] = "fingerprint_changed"
                    preflight["checkpoint"] = {
                        "phase": "preflight",
                        "code": "fingerprint_changed",
                    }
                if preflight["reason_code"] is not None:
                    await conn.execute(
                        """
                        UPDATE app_legacy_adoption_targets
                           SET actual_schema_fingerprint=$2,
                               included_tables=$3::jsonb,
                               excluded_tables=$4::jsonb,
                               missing_tables=$5::jsonb,
                               ownership_conflicts=$6::jsonb,
                               state='blocked', reason_code=$7,
                               checkpoint=$8::jsonb,
                               planned_metadata=$9::jsonb
                         WHERE id=$1
                        """,
                        target_id,
                        preflight["actual_schema_fingerprint"],
                        json.dumps(preflight["included_tables"], separators=(",", ":")),
                        json.dumps(preflight["excluded_tables"], separators=(",", ":")),
                        json.dumps(preflight["missing_tables"], separators=(",", ":")),
                        json.dumps(preflight["ownership_conflicts"], separators=(",", ":")),
                        preflight["reason_code"],
                        json.dumps(preflight["checkpoint"], separators=(",", ":")),
                        json.dumps(preflight["planned_metadata"], separators=(",", ":")),
                    )
                    await _record_ledger_audit(
                        conn,
                        app_id=app_id,
                        adoption_id=adoption_id,
                        target_id=target_id,
                        installation_id=(
                            preflight["installation_id"]
                            or next(iter(preflight["ownership_conflict_installations"]), None)
                        ),
                        vault_id=target_row["vault_id"],
                        release_id=plan["baseline_release_id"],
                        action="target_blocked",
                        outcome="error",
                        reason_code=preflight["reason_code"],
                        correlation_id=correlation_id,
                    )
                    if preflight["reason_code"] == "ownership_conflict":
                        await _record_ledger_audit(
                            conn,
                            app_id=app_id,
                            adoption_id=adoption_id,
                            target_id=target_id,
                            installation_id=(
                                preflight["installation_id"]
                                or next(iter(preflight["ownership_conflict_installations"]), None)
                            ),
                            vault_id=target_row["vault_id"],
                            release_id=plan["baseline_release_id"],
                            action="ownership_denied",
                            outcome="error",
                            reason_code="ownership_conflict",
                            correlation_id=correlation_id,
                        )
                    await _refresh_plan_status(conn, adoption_id)
                    return

                installation = await conn.fetchrow(
                    """
                    INSERT INTO vault_app_installations
                        (app_id, vault_id, desired_release_id, current_release_id,
                         lifecycle, grant_generation)
                    VALUES ($1,$2,$3,$3,'active',0)
                    RETURNING id
                    """,
                    app_id,
                    target_row["vault_id"],
                    plan["baseline_release_id"],
                )
                assert installation is not None
                installation_id = installation["id"]
                for table_name in preflight["included_tables"]:
                    await conn.execute(
                        """
                        INSERT INTO app_owned_resources
                            (installation_id, vault_id, resource_kind, resource_key,
                             status, metadata)
                        VALUES ($1,$2,'table',$3,'owned',$4::jsonb)
                        """,
                        installation_id,
                        target_row["vault_id"],
                        table_name,
                        json.dumps(
                            {
                                "source": "legacy_adoption",
                                "adoption_id": str(adoption_id),
                                "target_id": str(target_id),
                            },
                            separators=(",", ":"),
                        ),
                    )
                    await _record_ledger_audit(
                        conn,
                        app_id=app_id,
                        adoption_id=adoption_id,
                        target_id=target_id,
                        installation_id=installation_id,
                        vault_id=target_row["vault_id"],
                        release_id=plan["baseline_release_id"],
                        action="resource_adopted",
                        outcome="ok",
                        reason_code="adopted",
                        correlation_id=correlation_id,
                    )

                observed_checkpoint = {
                    "phase": "legacy_adoption",
                    "adoption_id": str(adoption_id),
                    "target_id": str(target_id),
                    "state": "baseline",
                }
                await conn.execute(
                    """
                    INSERT INTO app_installation_observed_states
                        (installation_id, app_id, vault_id, observed_generation,
                         observed_at, observed_release_id, observed_release_version,
                         schema_fingerprint, observed_grant_generation, checkpoint)
                    VALUES ($1,$2,$3,0,NOW(),$4,$5,$6,0,$7::jsonb)
                    """,
                    installation_id,
                    app_id,
                    target_row["vault_id"],
                    plan["baseline_release_id"],
                    release["version"],
                    preflight["actual_schema_fingerprint"],
                    json.dumps(observed_checkpoint, separators=(",", ":")),
                )
                await conn.execute(
                    """
                    UPDATE app_legacy_adoption_targets
                       SET actual_schema_fingerprint=$2,
                           included_tables=$3::jsonb,
                           excluded_tables=$4::jsonb,
                           missing_tables=$5::jsonb,
                           ownership_conflicts=$6::jsonb,
                           state='applied', reason_code=NULL,
                           installation_id=$7,
                           checkpoint=$8::jsonb,
                           planned_metadata=$9::jsonb
                     WHERE id=$1
                    """,
                    target_id,
                    preflight["actual_schema_fingerprint"],
                    json.dumps(preflight["included_tables"], separators=(",", ":")),
                    json.dumps(preflight["excluded_tables"], separators=(",", ":")),
                    json.dumps(preflight["missing_tables"], separators=(",", ":")),
                    json.dumps(preflight["ownership_conflicts"], separators=(",", ":")),
                    installation_id,
                    json.dumps(
                        {
                            "phase": "applied",
                            "adoption_id": str(adoption_id),
                            "target_id": str(target_id),
                            "grant_generation": 0,
                        },
                        separators=(",", ":"),
                    ),
                    json.dumps(preflight["planned_metadata"], separators=(",", ":")),
                )
                await _record_ledger_audit(
                    conn,
                    app_id=app_id,
                    adoption_id=adoption_id,
                    target_id=target_id,
                    installation_id=installation_id,
                    vault_id=target_row["vault_id"],
                    release_id=plan["baseline_release_id"],
                    action="target_applied",
                    outcome="ok",
                    reason_code="applied",
                    correlation_id=correlation_id,
                )
                await _refresh_plan_status(conn, adoption_id)
    except asyncpg.UniqueViolationError:
        # Another control-plane writer claimed the table between preflight and
        # the insert.  Re-enter through a short transaction to leave a bounded
        # blocked checkpoint; the pre-existing winner is never modified.
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM app_legacy_adoption_plans WHERE id=$1 AND app_id=$2 FOR UPDATE",
                    adoption_id,
                    app_id,
                )
                target = await conn.fetchrow(
                    "SELECT * FROM app_legacy_adoption_targets WHERE id=$1 AND adoption_id=$2 FOR UPDATE",
                    target_id,
                    adoption_id,
                )
                if row is not None and target is not None:
                    await conn.execute(
                        """
                        UPDATE app_legacy_adoption_targets
                           SET state='blocked',
                               reason_code='ownership_conflict',
                               checkpoint=$2::jsonb
                         WHERE id=$1
                        """,
                        target_id,
                        json.dumps(
                            {"phase": "preflight", "code": "ownership_conflict"},
                            separators=(",", ":"),
                        ),
                    )
                    await _record_ledger_audit(
                        conn,
                        app_id=app_id,
                        adoption_id=adoption_id,
                        target_id=target_id,
                        installation_id=None,
                        vault_id=target["vault_id"],
                        release_id=row["baseline_release_id"],
                        action="target_blocked",
                        outcome="error",
                        reason_code="ownership_conflict",
                        correlation_id=correlation_id,
                    )
                    await _refresh_plan_status(conn, adoption_id)


async def apply_legacy_adoption(
    app_id: uuid.UUID | str,
    adoption_id: uuid.UUID | str,
    *,
    user: AuthenticatedUser,
    correlation_id: str,
) -> dict[str, Any]:
    app_uuid = _as_uuid(app_id, field="app_id")
    adoption_uuid = _as_uuid(adoption_id, field="adoption_id")
    actor_kind = await _authorize_existing_plan(
        user,
        app_uuid,
        adoption_uuid,
        action="app.legacy_adoption.apply",
        correlation_id=correlation_id,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        ids = await conn.fetch(
            "SELECT id FROM app_legacy_adoption_targets WHERE adoption_id=$1 ORDER BY target_order",
            adoption_uuid,
        )
    for row in ids:
        await _apply_target(
            app_uuid,
            adoption_uuid,
            row["id"],
            correlation_id=correlation_id,
        )
    async with pool.acquire() as conn:
        result = await _load_projection(conn, app_uuid, adoption_uuid)
    record_app_audit(
        "app.legacy_adoption.apply",
        correlation_id=correlation_id,
        outcome="ok",
        reason="applied" if result["status"] == "applied" else "resumed",
        actor=actor_kind,
        actor_id=user.user_id,
        app_id=app_uuid,
    )
    result["outcome"] = "applied" if result["status"] == "applied" else "resumed"
    return result
