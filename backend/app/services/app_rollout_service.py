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
from app.services import app_resource_service
from app.repositories import table_data_repo
from app.services.auth_service import AuthenticatedUser
from app.util.text import to_nfc_any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PHASES = {"expand": 0, "backfill": 1, "enforce": 2}
_ALLOWED = {
    "create_table",
    "add_column",
    "add_unique_key",
    "add_index",
    "backfill_column",
    "set_not_null",
}
_REASON = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SOURCE_REVISION = re.compile(r"^[0-9a-fA-F]{40,64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_COLUMN_FIELDS = {
    "name",
    "type",
    "required",
    "default",
    "check",
    "enum",
    "references",
    "on_delete",
    "unique",
    "index",
}
_MAX_IDENTIFIER_LENGTH = 63
_MAX_TABLES = 256
_MAX_COLUMNS = 256
_MAX_SCHEMA_META = 256
_MAX_PLANS = 256
_MAX_STEPS = 256


def _canonical_json(value: Any) -> bytes:
    return app_resource_service.canonical_json(value)


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
    return {
        key: value
        for key, value in step.items()
        if key not in {"checksum", "step_order"}
    }


def _reject_unlisted_fields(value: dict[str, Any], allowed: set[str], *, label: str) -> None:
    if set(value) - allowed:
        raise ValidationError(f"{label} contains an unsupported field")


def _table_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_LENGTH
        or not _TABLE_NAME.fullmatch(value)
    ):
        raise ValidationError("Manifest table must be a safe table name")
    return value


def _column_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_LENGTH
        or not _TABLE_NAME.fullmatch(value)
    ):
        raise ValidationError("Manifest column must be a safe column name")
    return value


def _named_identifier(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_LENGTH
        or not _TABLE_NAME.fullmatch(value)
    ):
        raise ValidationError(f"Manifest {label} name is invalid")
    return value


def _normalize_column(value: Any, *, nullable_only: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Manifest column must be an object")
    _reject_unlisted_fields(value, _ALLOWED_COLUMN_FIELDS, label="Manifest column")
    if "name" not in value:
        raise ValidationError("Manifest column is required")
    if "type" not in value:
        raise ValidationError("Manifest column type is required")
    result = dict(to_nfc_any(value))
    result["name"] = _column_name(result["name"])
    for flag in ("required", "unique", "index"):
        if flag in result and not isinstance(result[flag], bool):
            raise ValidationError(f"Manifest column {flag} must be boolean")
    check = result.get("check")
    if check is not None:
        if not isinstance(check, dict) or set(check) - {"op", "value", "values"}:
            raise ValidationError("Manifest column check contains an unsupported field")
    references = result.get("references")
    if references is not None:
        if not isinstance(references, dict) or set(references) - {"table", "column", "on_delete"}:
            raise ValidationError(
                "Manifest column references contains an unsupported field"
            )
    if nullable_only and any(
        result.get(key) not in (None, False)
        for key in ("required", "unique", "index", "default", "check", "enum", "references", "on_delete")
    ):
        raise ValidationError("add_column only permits a nullable unconstrained column")
    default = result.get("default")
    if isinstance(default, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(\)", default.strip()):
        raise ValidationError("Manifest column defaults must not contain expressions")
    result = table_data_repo.normalize_column_spec(result)
    for flag in ("required", "unique", "index"):
        if result.get(flag) is False or result.get(flag) is None:
            result.pop(flag, None)
    for key in ("default", "check", "enum", "references", "on_delete"):
        if result.get(key) is None:
            result.pop(key, None)
    return result


def _normalize_unique_keys(value: Any, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValidationError("Manifest unique_keys must be an array")
    if len(value) > _MAX_SCHEMA_META:
        raise ValidationError("Manifest unique_keys is too large")
    names = {column["name"] for column in columns}
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("Manifest unique key must be an object")
        _reject_unlisted_fields(item, {"name", "columns"}, label="Manifest unique key")
        raw_columns = item.get("columns")
        if (
            not isinstance(raw_columns, list)
            or not raw_columns
            or len(raw_columns) > _MAX_SCHEMA_META
        ):
            raise ValidationError("Manifest unique key columns must be non-empty")
        key_columns = [_column_name(column) for column in raw_columns]
        if any(column not in names for column in key_columns) or len(set(key_columns)) != len(key_columns):
            raise ValidationError("Manifest unique key references an invalid column")
        identity = tuple(key_columns)
        if identity in seen:
            raise ValidationError("Manifest unique keys must be distinct")
        seen.add(identity)
        normalized: dict[str, Any] = {"columns": key_columns}
        if item.get("name") is not None:
            normalized["name"] = _named_identifier(
                item["name"], label="unique key"
            )
        result.append(normalized)
    return result


def _normalize_indexes(value: Any, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValidationError("Manifest indexes must be an array")
    if len(value) > _MAX_SCHEMA_META:
        raise ValidationError("Manifest indexes is too large")
    names = {column["name"] for column in columns}
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("Manifest index must be an object")
        _reject_unlisted_fields(item, {"name", "columns"}, label="Manifest index")
        raw_columns = item.get("columns")
        if (
            not isinstance(raw_columns, list)
            or not raw_columns
            or len(raw_columns) > _MAX_SCHEMA_META
        ):
            raise ValidationError("Manifest index columns must be non-empty")
        index_columns: list[dict[str, str]] = []
        for raw_column in raw_columns:
            raw_name: Any
            raw_order: Any
            if isinstance(raw_column, str):
                raw_name, raw_order = raw_column, "asc"
            elif isinstance(raw_column, dict):
                _reject_unlisted_fields(raw_column, {"name", "order"}, label="Manifest index column")
                raw_name, raw_order = raw_column.get("name"), raw_column.get("order", "asc")
            else:
                raise ValidationError("Manifest index column is invalid")
            if not isinstance(raw_name, str):
                raise ValidationError("Manifest index column is invalid")
            name = _column_name(raw_name)
            if name not in names or not isinstance(raw_order, str) or raw_order.lower() not in {"asc", "desc"}:
                raise ValidationError("Manifest index column is invalid")
            index_columns.append({"name": name, "order": raw_order.lower()})
        identity = tuple((column["name"], column["order"]) for column in index_columns)
        if identity in seen:
            raise ValidationError("Manifest indexes must be distinct")
        seen.add(identity)
        normalized_item: dict[str, Any] = {"columns": index_columns}
        if item.get("name") is not None:
            normalized_item["name"] = _named_identifier(
                item["name"], label="index"
            )
        result.append(normalized_item)
    return result


def _normalize_table(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Manifest table descriptor must be an object")
    _reject_unlisted_fields(value, {"name", "columns", "unique_keys", "indexes"}, label="Manifest table")
    name = _table_name(value.get("name"))
    if "unique_keys" not in value or "indexes" not in value:
        raise ValidationError(
            "Manifest table requires the complete unique_keys and indexes descriptor"
        )
    raw_columns = value.get("columns")
    if not isinstance(raw_columns, list):
        raise ValidationError("Manifest table columns must be an array")
    if len(raw_columns) > _MAX_COLUMNS:
        raise ValidationError("Manifest table columns is too large")
    columns = [_normalize_column(column) for column in raw_columns]
    if len({column["name"] for column in columns}) != len(columns):
        raise ValidationError("Manifest table columns must be distinct")
    normalized_unique_keys = _normalize_unique_keys(value.get("unique_keys"), columns)
    normalized_indexes = _normalize_indexes(value.get("indexes"), columns)
    inline_unique = [
        {"columns": [column["name"]]}
        for column in columns
        if column.get("unique") is True
    ]
    inline_indexes = [
        {"columns": [{"name": column["name"], "order": "asc"}]}
        for column in columns
        if column.get("index") is True
    ]
    for column in columns:
        column.pop("unique", None)
        column.pop("index", None)
    unique_identities = {tuple(item["columns"]) for item in normalized_unique_keys}
    normalized_unique_keys.extend(
        item for item in inline_unique if tuple(item["columns"]) not in unique_identities
    )
    index_identities = {
        tuple((column["name"], column["order"]) for column in item["columns"])
        for item in normalized_indexes
    }
    normalized_indexes.extend(
        item
        for item in inline_indexes
        if tuple((column["name"], column["order"]) for column in item["columns"])
        not in index_identities
    )
    normalized_unique_keys.sort(
        key=lambda item: app_resource_service.canonical_json(item["columns"])
    )
    normalized_indexes.sort(
        key=lambda item: app_resource_service.canonical_json(item["columns"])
    )
    columns.sort(key=lambda column: column["name"])
    return {
        "name": name,
        "columns": columns,
        "unique_keys": normalized_unique_keys,
        "indexes": normalized_indexes,
    }


def _normalize_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Manifest schema must be an object")
    _reject_unlisted_fields(value, {"tables", "fingerprint"}, label="Manifest schema")
    raw_tables = value.get("tables")
    if not isinstance(raw_tables, list):
        raise ValidationError("Manifest schema tables must be an array")
    if len(raw_tables) > _MAX_TABLES:
        raise ValidationError("Manifest schema tables is too large")
    tables = [_normalize_table(table) for table in raw_tables]
    if len({table["name"] for table in tables}) != len(tables):
        raise ValidationError("Manifest schema table names must be unique")
    tables.sort(key=lambda table: table["name"])
    computed = app_resource_service.canonical_table_fingerprint(tables)
    supplied = value.get("fingerprint")
    if supplied is not None and _hex(supplied, field="schema fingerprint") != computed:
        raise ValidationError("Manifest schema fingerprint mismatch")
    return {"tables": tables, "fingerprint": computed}


def _normalize_source(value: Any) -> str | dict[str, str]:
    if value == "fresh":
        return "fresh"
    if not isinstance(value, dict):
        raise ValidationError("Transition plan source must be fresh or an exact source object")
    _reject_unlisted_fields(value, {"release_version", "schema_fingerprint"}, label="Transition plan source")
    release_version = value.get("release_version")
    if not isinstance(release_version, str) or not _SEMVER.fullmatch(release_version):
        raise ValidationError("Transition plan source release_version is invalid")
    fingerprint = _hex(value.get("schema_fingerprint"), field="source schema fingerprint")
    return {"release_version": release_version, "schema_fingerprint": fingerprint}


def _normalize_step(step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ValidationError(f"Manifest step {index + 1} must be an object")
    _reject_unlisted_fields(step, {"id", "phase", "operation", "payload", "checksum"}, label="Manifest step")
    step_id = step.get("id")
    if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
        raise ValidationError("Manifest step id is invalid")
    phase = step.get("phase")
    if not isinstance(phase, str) or phase not in _PHASES:
        raise ValidationError("Manifest step phase is invalid")
    operation = step.get("operation")
    if not isinstance(operation, str) or operation not in _ALLOWED:
        raise ValidationError("Manifest contains an unsupported rollout operation")
    expected_phase = {
        "create_table": "expand",
        "add_column": "expand",
        "add_unique_key": "expand",
        "add_index": "expand",
        "backfill_column": "backfill",
        "set_not_null": "enforce",
    }[operation]
    if phase != expected_phase:
        raise ValidationError("Manifest operation is not allowed in this phase")
    checksum = _hex(step.get("checksum"), field="step checksum")
    payload = step.get("payload")
    if not isinstance(payload, dict):
        raise ValidationError("Manifest step payload must be an object")
    payload = to_nfc_any(payload)
    table = _table_name(payload.get("table"))
    if operation == "create_table":
        if any(key not in payload for key in ("columns", "unique_keys", "indexes")):
            raise ValidationError(
                "create_table requires the complete columns, unique_keys, and indexes descriptor"
            )
        descriptor = dict(payload)
        descriptor["name"] = table
        descriptor.pop("table", None)
        normalized_descriptor = _normalize_table(descriptor)
        normalized_payload = {"table": table, **{key: normalized_descriptor[key] for key in ("columns", "unique_keys", "indexes")}}
    elif operation == "add_column":
        _reject_unlisted_fields(payload, {"table", "column"}, label="add_column")
        normalized_payload = {"table": table, "column": _normalize_column(payload.get("column"), nullable_only=True)}
    elif operation in {"add_unique_key", "add_index"}:
        allowed = {"table", "name", "columns"}
        _reject_unlisted_fields(payload, allowed, label=operation)
        raw_columns = payload.get("columns")
        if (
            not isinstance(raw_columns, list)
            or not raw_columns
            or len(raw_columns) > _MAX_SCHEMA_META
        ):
            raise ValidationError(f"{operation} columns must be non-empty")
        if operation == "add_unique_key":
            key_columns = [_column_name(column) for column in raw_columns]
            if len(set(key_columns)) != len(key_columns):
                raise ValidationError("add_unique_key columns must be distinct")
            normalized_payload = {"table": table, "columns": key_columns}
            if payload.get("name") is not None:
                name = payload["name"]
                normalized_payload["name"] = _named_identifier(
                    name, label="unique key"
                )
        else:
            index_columns: list[dict[str, str]] = []
            seen: set[str] = set()
            for raw_column in raw_columns:
                raw_name: Any
                raw_order: Any
                if isinstance(raw_column, str):
                    raw_name, raw_order = raw_column, "asc"
                elif isinstance(raw_column, dict):
                    _reject_unlisted_fields(raw_column, {"name", "order"}, label="Manifest index column")
                    raw_name, raw_order = raw_column.get("name"), raw_column.get("order", "asc")
                else:
                    raise ValidationError("Manifest index column is invalid")
                if not isinstance(raw_name, str):
                    raise ValidationError("Manifest index column is invalid")
                name = _column_name(raw_name)
                if not isinstance(raw_order, str) or raw_order.lower() not in {"asc", "desc"} or name in seen:
                    raise ValidationError("Manifest index column is invalid")
                seen.add(name)
                index_columns.append({"name": name, "order": raw_order.lower()})
            normalized_payload = {"table": table, "columns": index_columns}
            if payload.get("name") is not None:
                name = payload["name"]
                normalized_payload["name"] = _named_identifier(name, label="index")
    elif operation == "backfill_column":
        _reject_unlisted_fields(payload, {"table", "column", "primary_key", "where_null", "batch_size", "value"}, label="backfill_column")
        column = _column_name(payload.get("column"))
        if payload.get("where_null") is not True:
            raise ValidationError("backfill_column requires where_null=true")
        batch_size = payload.get("batch_size")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 1000:
            raise ValidationError("backfill_column batch_size must be between 1 and 1000")
        primary_key = _column_name(payload.get("primary_key", "id"))
        if primary_key != "id":
            raise ValidationError("backfill_column cursor must use the stable id primary key")
        value = payload.get("value")
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise ValidationError("backfill_column value must be a non-null scalar")
        normalized_payload = {"table": table, "column": column, "primary_key": primary_key, "batch_size": batch_size, "where_null": True, "value": value}
    else:
        _reject_unlisted_fields(payload, {"table", "column"}, label="set_not_null")
        normalized_payload = {"table": table, "column": _column_name(payload.get("column"))}
    canonical_step = {
        "id": step_id,
        "phase": phase,
        "operation": operation,
        "payload": normalized_payload,
    }
    if _digest(canonical_step) != checksum:
        raise ValidationError("Manifest step checksum mismatch")
    return {"id": step_id, "phase": phase, "operation": operation, "payload": normalized_payload, "checksum": checksum, "step_order": index}


def _normalize_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Transition plan must be an object")
    _reject_unlisted_fields(value, {"source", "steps"}, label="Transition plan")
    steps = value.get("steps")
    if not isinstance(steps, list):
        raise ValidationError("Transition plan steps must be an array")
    if len(steps) > _MAX_STEPS:
        raise ValidationError("Transition plan steps is too large")
    normalized_steps: list[dict[str, Any]] = []
    ids: set[str] = set()
    previous_phase = -1
    for index, raw in enumerate(steps):
        step = _normalize_step(raw, index)
        if step["id"] in ids:
            raise ValidationError("Transition plan step ids must be unique")
        ids.add(step["id"])
        phase_order = _PHASES[step["phase"]]
        if phase_order < previous_phase:
            raise ValidationError("Transition plan steps must be ordered by phase")
        previous_phase = phase_order
        normalized_steps.append(step)
    return {"source": _normalize_source(value.get("source")), "steps": normalized_steps}


def _manifest_checksum_payload(
    manifest: dict[str, Any], *, product_version: str | None = None
) -> dict[str, Any]:
    payload = {
        "manifest_version": manifest["manifest_version"],
        "app_key": manifest["app_key"],
        "source_revision": manifest["source_revision"],
        "image_digest": manifest["image_digest"],
        "schema_version": manifest["schema_version"],
        "schema": manifest["schema"],
        "transition_plans": [
            {
                "source": plan["source"],
                "steps": [_step_without_checksum(step) for step in plan["steps"]],
            }
            for plan in manifest["transition_plans"]
        ],
    }
    if product_version is not None:
        if not isinstance(product_version, str) or not _SEMVER.fullmatch(product_version):
            raise ValidationError("product version is invalid")
        payload["product_version"] = product_version
    return payload


def _normalize_manifest(
    manifest: Any, *, product_version: str | None = None
) -> tuple[dict[str, Any], str]:
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("Manifest must be an object") from exc
    if not isinstance(manifest, dict):
        raise ValidationError("Manifest must be an object")
    fields = {"manifest_version", "app_key", "source_revision", "image_digest", "schema_version", "schema", "transition_plans"}
    _reject_unlisted_fields(manifest, fields, label="Manifest")
    if type(manifest.get("manifest_version")) is not int or manifest["manifest_version"] != 2:
        raise ValidationError("Only manifest_version=2 is supported")
    app_key = manifest.get("app_key")
    if not isinstance(app_key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", app_key):
        raise ValidationError("Manifest app_key is invalid")
    source_revision = manifest.get("source_revision")
    if not isinstance(source_revision, str) or not _SOURCE_REVISION.fullmatch(source_revision):
        raise ValidationError("Manifest source_revision must be a full revision")
    image_digest = manifest.get("image_digest")
    if not isinstance(image_digest, str) or not _IMAGE_DIGEST.fullmatch(image_digest):
        raise ValidationError("Manifest image_digest must be an immutable sha256 digest")
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise ValidationError("Manifest schema_version must be a positive integer")
    schema = _normalize_schema(manifest.get("schema"))
    raw_plans = manifest.get("transition_plans")
    if not isinstance(raw_plans, list) or not raw_plans:
        raise ValidationError("Manifest transition_plans must be a non-empty array")
    if len(raw_plans) > _MAX_PLANS:
        raise ValidationError("Manifest transition_plans is too large")
    plans = [_normalize_plan(plan) for plan in raw_plans]
    plans.sort(key=lambda plan: (0, "") if plan["source"] == "fresh" else (1, _canonical_json(plan["source"])))
    if sum(plan["source"] == "fresh" for plan in plans) != 1:
        raise ValidationError("Manifest must contain exactly one fresh transition plan")
    source_keys = ["fresh" if plan["source"] == "fresh" else _canonical_json(plan["source"]) for plan in plans]
    if len(set(source_keys)) != len(source_keys):
        raise ValidationError("Manifest transition plan sources must be unique")
    fresh = next(plan for plan in plans if plan["source"] == "fresh")
    desired_by_name = {
        table["name"]: table for table in schema["tables"]
    }
    fresh_by_name: dict[str, dict[str, Any]] = {}
    for step in fresh["steps"]:
        if step["operation"] != "create_table":
            raise ValidationError("Fresh transition plan must contain only create_table operations")
        payload = step["payload"]
        table = payload["table"]
        if table in fresh_by_name:
            raise ValidationError("Fresh transition plan must create each table once")
        fresh_by_name[table] = app_resource_service.canonical_table_descriptor(
            {
                "name": table,
                "columns": payload["columns"],
                "unique_keys": payload["unique_keys"],
                "indexes": payload["indexes"],
            }
        )
    if set(fresh_by_name) != set(desired_by_name):
        raise ValidationError("Fresh transition plan must cover the complete desired schema")
    for table_name, desired in desired_by_name.items():
        if fresh_by_name[table_name] != app_resource_service.canonical_table_descriptor(desired):
            raise ValidationError("Fresh transition plan does not match the desired schema")
    normalized = {
        "manifest_version": 2,
        "app_key": to_nfc_any(app_key),
        "source_revision": source_revision.lower(),
        "image_digest": image_digest,
        "schema_version": schema_version,
        "schema": schema,
        "transition_plans": plans,
    }
    return normalized, _digest(
        _manifest_checksum_payload(normalized, product_version=product_version)
    )


def validate_manifest(
    manifest: Any,
    manifest_checksum: str | None = None,
    *,
    version: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize one strict App Release Manifest v2."""
    normalized, computed = _normalize_manifest(manifest, product_version=version)
    if manifest_checksum is None:
        raise ValidationError("Manifest checksum is required")
    if _hex(manifest_checksum, field="manifest checksum") != computed:
        raise ValidationError("Manifest checksum mismatch")
    return {**normalized, "checksum": computed}


def manifest_storage_projection(manifest: dict[str, Any]) -> dict[str, Any]:
    """Remove worker-only ordering fields before persisting a manifest."""
    return {
        **{
            key: value
            for key, value in manifest.items()
            if key not in {"checksum"}
        },
        "transition_plans": [
            {
                "source": plan["source"],
                "steps": [
                    {
                        key: value
                        for key, value in step.items()
                        if key != "step_order"
                    }
                    for step in plan["steps"]
                ],
            }
            for plan in manifest["transition_plans"]
        ],
    }


def manifest_checksum(manifest: Any, *, version: str | None = None) -> str:
    """Return the canonical v2 checksum for a manifest without its outer checksum."""
    _normalized, computed = _normalize_manifest(manifest, product_version=version)
    return computed


def schema_projection_fingerprint(schema: Any) -> str:
    if not isinstance(schema, dict) or not isinstance(schema.get("tables"), list):
        raise ValidationError("Manifest schema must contain a tables array")
    return app_resource_service.canonical_table_fingerprint(schema["tables"])


def select_transition_plan(
    manifest: dict[str, Any],
    *,
    current_release_version: str | None,
    current_schema_fingerprint: str | None,
) -> dict[str, Any]:
    """Select exactly one fresh or exact release+schema transition plan."""
    plans = manifest.get("transition_plans")
    if not isinstance(plans, list):
        raise ValidationError("Manifest transition plan is unavailable")
    if current_release_version is None and current_schema_fingerprint is None:
        candidates = [plan for plan in plans if plan.get("source") == "fresh"]
    elif current_release_version is None or current_schema_fingerprint is None:
        raise ValidationError("Exact transition plan source is unavailable")
    else:
        fingerprint = _hex(current_schema_fingerprint, field="current schema fingerprint")
        candidates = [
            plan
            for plan in plans
            if isinstance(plan.get("source"), dict)
            and plan["source"].get("release_version") == current_release_version
            and plan["source"].get("schema_fingerprint") == fingerprint
        ]
    if len(candidates) != 1:
        raise ValidationError("Exact transition plan source is missing or ambiguous")
    return candidates[0]


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
                    "SELECT id, version, manifest, manifest_checksum FROM app_releases WHERE app_id=$1 AND id=$2",
                    app_id,
                    release_id,
                )
                if release is None:
                    raise NotFoundError("Release", "not found")
                if release["manifest_checksum"] != checksum:
                    raise ConflictError("Release checksum does not match request")
                normalized = validate_manifest(
                    release["manifest"], checksum, version=release["version"]
                )
                app_key = await conn.fetchval(
                    "SELECT app_key FROM app_definitions WHERE id=$1", app_id
                )
                if app_key is None:
                    raise NotFoundError("App", "not found")
                if normalized["app_key"] != app_key:
                    raise ConflictError("Release manifest app_key does not match the app definition")
                targets = await conn.fetch(
                    """
                    SELECT i.id AS installation_id, i.vault_id, i.current_release_id,
                           i.desired_release_id,
                           current_release.version AS current_version,
                           i.grant_generation, i.lifecycle,
                           g.status AS grant_status, g.generation AS active_generation,
                           o.observed_release_id, o.observed_grant_generation,
                           o.observed_generation, o.schema_fingerprint
                      FROM vault_app_installations i
                      LEFT JOIN app_releases AS current_release
                        ON current_release.id = i.current_release_id
                      LEFT JOIN LATERAL (
                          SELECT status, generation FROM installation_grants
                           WHERE installation_id=i.id AND status='active'
                           ORDER BY generation DESC LIMIT 1
                      ) g ON TRUE
                      LEFT JOIN app_installation_observed_states o ON o.installation_id=i.id
                     WHERE i.app_id=$1 AND i.lifecycle IN ('active','installing')
                       AND i.current_release_id IS DISTINCT FROM $2
                     ORDER BY i.created_at, i.id
                    """,
                    app_id,
                    release_id,
                )
                if not targets:
                    raise ValidationError("No installation requires this rollout")
                plans_by_installation: dict[uuid.UUID, dict[str, Any]] = {}
                for target in targets:
                    if target["grant_status"] != "active" or target["active_generation"] != target["grant_generation"]:
                        raise ConflictError("Rollout preflight failed")
                    fresh = target["current_release_id"] is None
                    if fresh:
                        if (
                            target["lifecycle"] != "installing"
                            or target["desired_release_id"] != release_id
                        ):
                            raise ConflictError("Rollout fresh-install preflight failed")
                    elif (
                        target["observed_generation"] is None
                        or target["observed_release_id"] != target["current_release_id"]
                        or target["observed_grant_generation"] != target["grant_generation"]
                    ):
                        raise ConflictError("Rollout preflight failed")
                    try:
                        plan = select_transition_plan(
                            normalized,
                            current_release_version=None if fresh else target["current_version"],
                            current_schema_fingerprint=None if fresh else target["schema_fingerprint"],
                        )
                    except ValidationError as exc:
                        raise ConflictError("Rollout source transition plan is unavailable") from exc
                    plans_by_installation[target["installation_id"]] = plan
                    for step in plan["steps"]:
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
                    for step in plans_by_installation[target["installation_id"]]["steps"]:
                        await conn.execute(
                            """INSERT INTO app_rollout_steps(job_id, target_id, installation_id, release_id, step_id, step_order, step_checksum, operation) VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
                            job["id"], rollout_target["id"], target["installation_id"], release_id, step["id"], step["step_order"], step["checksum"], step["operation"],
                        )
                    await conn.execute(
                        """
                        UPDATE vault_app_installations
                           SET desired_release_id=$2,
                               lifecycle=CASE WHEN current_release_id IS NULL
                                              THEN 'installing' ELSE 'upgrading' END,
                               blocked_reason=NULL
                         WHERE id=$1 AND app_id=$3
                        """,
                        target["installation_id"],
                        release_id,
                        app_id,
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
                    "SELECT id, version, manifest, manifest_checksum FROM app_releases WHERE app_id=$1 AND id=$2",
                    app_uuid,
                    release_uuid,
                )
                if release is None:
                    raise NotFoundError("Release", "not found")
                if release["manifest_checksum"] != checksum:
                    raise ConflictError("Release checksum does not match request")
                normalized = validate_manifest(
                    release["manifest"], checksum, version=release["version"]
                )
                app_key = await conn.fetchval(
                    "SELECT app_key FROM app_definitions WHERE id=$1", app_uuid
                )
                if app_key is None:
                    raise NotFoundError("App", "not found")
                if normalized["app_key"] != app_key:
                    raise ConflictError("Release manifest app_key does not match the app definition")
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

                # The immutable source target rows define identity.  A blocked
                # source can leave the failed target blocked while untouched
                # targets remain upgrading; active targets may already have
                # converged.  Lifecycle is therefore bounded to those states
                # without narrowing the source target set by desired release.
                installations = await conn.fetch(
                    """
                    SELECT i.id AS installation_id, i.vault_id, i.current_release_id,
                           current_release.version AS current_version,
                           i.desired_release_id, i.grant_generation, i.lifecycle,
                           g.generation AS active_generation, g.status AS grant_status,
                           o.observed_release_id, o.observed_grant_generation,
                           o.observed_generation, o.schema_fingerprint, st.ordinal
                      FROM vault_app_installations AS i
                      JOIN app_rollout_targets AS st
                        ON st.job_id=$2 AND st.installation_id=i.id
                      LEFT JOIN LATERAL (
                          SELECT generation, status
                            FROM installation_grants
                           WHERE installation_id=i.id AND status='active'
                           ORDER BY generation DESC LIMIT 1
                      ) AS g ON TRUE
                      LEFT JOIN app_installation_observed_states AS o
                        ON o.installation_id=i.id
                      LEFT JOIN app_releases AS current_release
                        ON current_release.id=i.current_release_id
                     WHERE i.app_id=$1
                       AND i.lifecycle IN ('blocked', 'installing', 'upgrading', 'active')
                     ORDER BY st.ordinal, i.id
                     FOR UPDATE OF i
                    """,
                    app_uuid,
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
                plans_by_installation: dict[uuid.UUID, dict[str, Any]] = {}
                for target in installations:
                    if target["grant_status"] != "active" or target["active_generation"] != target["grant_generation"]:
                        raise ConflictError("Resume preflight failed")
                    fresh = target["current_release_id"] is None
                    if fresh:
                        if (
                            target["lifecycle"] not in {"blocked", "installing"}
                            or target["desired_release_id"] != release_uuid
                        ):
                            raise ConflictError("Resume fresh-install preflight failed")
                    elif (
                        target["observed_generation"] is None
                        or target["observed_release_id"] != target["current_release_id"]
                        or target["observed_grant_generation"] != target["grant_generation"]
                    ):
                        raise ConflictError("Resume preflight failed")
                    if target["current_release_id"] == release_uuid:
                        continue
                    try:
                        plan = select_transition_plan(
                            normalized,
                            current_release_version=None if fresh else target["current_version"],
                            current_schema_fingerprint=None if fresh else target["schema_fingerprint"],
                        )
                    except ValidationError as exc:
                        raise ConflictError("Resume source transition plan is unavailable") from exc
                    plans_by_installation[target["installation_id"]] = plan
                    for step in plan["steps"]:
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
                    for step in plans_by_installation[target["installation_id"]]["steps"]:
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
                           SET desired_release_id=$2,
                               lifecycle=CASE WHEN current_release_id IS NULL
                                              THEN 'installing' ELSE 'upgrading' END,
                               blocked_reason=NULL
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
