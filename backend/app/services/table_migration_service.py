"""Structured, idempotent table migrations for vault tables.

Translates a JSON operation list into `alter_table` calls, applied
atomically under one transaction and deduplicated by Idempotency-Key.
Extracted from `table_service`; `alter_table` and `index_table_metadata`
stay there and are imported here (one-directional dependency).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, cast

from app.db.postgres import get_pool
from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.repositories import table_registry_repo
from app.repositories.events_repo import emit_event
from app.services.table_service import alter_table, index_table_metadata
from app.services.uri_service import vault_uri
from app.util.text import to_nfc_any

logger = logging.getLogger("akb.tables")


_MIGRATION_COLUMN_KEYS = {
    "name",
    "type",
    "required",
    "default",
    "check",
    "enum",
    "unique",
    "index",
    "references",
    "on_delete",
}

_MIGRATION_ALTER_COLUMN_KEYS = {
    "name",
    "set_default",
    "default",
    "drop_default",
    "set_check",
    "check",
    "drop_check",
    "set_not_null",
    "drop_not_null",
    "set_enum",
    "enum",
    "rename_enum_values",
    "enum_renames",
}


def table_migration_checksum(operations: list[dict]) -> str:
    """Return the deterministic checksum for a structured migration op list."""
    operations = _normalize_migration_operations(operations)
    payload = json.dumps(
        operations,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_migration_operations(operations: list[dict]) -> list[dict]:
    normalized = to_nfc_any(operations)
    _validate_migration_operations(normalized)
    return cast(list[dict], normalized)


def _validate_migration_operations(operations: list[dict]) -> None:
    if not isinstance(operations, list) or not operations:
        raise ValidationError("Migration body must be a non-empty JSON array of operations.")
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            raise ValidationError(f"Migration operation #{index + 1} must be an object.")


def _validate_idempotency_key(idempotency_key: str | None) -> str:
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValidationError("Idempotency-Key header is required for table migrations.")
    value = idempotency_key.strip()
    try:
        parsed = uuid.UUID(value)
    except ValueError as e:
        raise ValidationError("Idempotency-Key header must be a UUID.") from e
    return str(parsed)


def _migration_table_name(op: dict) -> str:
    table_name = op.get("table") or op.get("table_name")
    if not isinstance(table_name, str) or not table_name:
        raise ValidationError("Migration operation requires a non-empty table/table_name.")
    return table_name


def _column_spec_from_migration_op(op: dict, *, keys: set[str], ctx: str) -> dict:
    column = op.get("column")
    if isinstance(column, dict):
        spec = dict(column)
    else:
        spec = {key: op[key] for key in keys if key in op}
        if isinstance(column, str) and "name" not in spec:
            spec["name"] = column
    if not spec:
        raise ValidationError(f"{ctx} requires a column object or column fields.")
    return spec


def _object_spec_from_migration_op(op: dict, *, field: str, keys: set[str], ctx: str) -> dict:
    value = op.get(field)
    if isinstance(value, dict):
        spec = dict(value)
    else:
        spec = {key: op[key] for key in keys if key in op}
    if not spec:
        raise ValidationError(f"{ctx} requires a {field} object or fields.")
    return spec


def _name_from_migration_op(op: dict, *fields: str, ctx: str) -> str:
    for field in fields:
        if field not in op:
            continue
        value = op[field]
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str) and value:
            return value
    raise ValidationError(f"{ctx} requires a non-empty name.")


def _migration_op_to_alter_kwargs(op: dict) -> tuple[str, dict[str, Any]]:
    raw_op = op.get("op")
    if not isinstance(raw_op, str) or not raw_op:
        raise ValidationError("Migration operation requires a non-empty op.")
    op_name = raw_op.strip().lower().replace("-", "_")
    table_name = _migration_table_name(op)

    if op_name == "add_column":
        return table_name, {
            "add_columns": [
                _column_spec_from_migration_op(
                    op,
                    keys=_MIGRATION_COLUMN_KEYS,
                    ctx="add_column",
                )
            ],
        }
    if op_name == "alter_column":
        return table_name, {
            "alter_columns": [
                _column_spec_from_migration_op(
                    op,
                    keys=_MIGRATION_ALTER_COLUMN_KEYS,
                    ctx="alter_column",
                )
            ],
        }
    if op_name == "drop_column":
        return table_name, {
            "drop_columns": [
                _name_from_migration_op(op, "name", "column", ctx="drop_column")
            ],
        }
    if op_name == "rename_column":
        old_name = _name_from_migration_op(
            op, "from", "old_name", "from_name", "old", "column", ctx="rename_column.from"
        )
        new_name = _name_from_migration_op(
            op, "to", "new_name", "to_name", "new", ctx="rename_column.to"
        )
        return table_name, {"rename_columns": {old_name: new_name}}
    if op_name == "add_unique_key":
        return table_name, {
            "add_unique_keys": [
                _object_spec_from_migration_op(
                    op,
                    field="unique_key",
                    keys={"name", "columns"},
                    ctx="add_unique_key",
                )
            ],
        }
    if op_name == "drop_unique_key":
        return table_name, {
            "drop_unique_keys": [
                _name_from_migration_op(op, "name", "unique_key", ctx="drop_unique_key")
            ],
        }
    if op_name == "add_index":
        return table_name, {
            "add_indexes": [
                _object_spec_from_migration_op(
                    op,
                    field="index",
                    keys={"name", "columns"},
                    ctx="add_index",
                )
            ],
        }
    if op_name == "drop_index":
        return table_name, {
            "drop_indexes": [
                _name_from_migration_op(op, "name", "index", ctx="drop_index")
            ],
        }
    raise ValidationError(
        "Unsupported migration op "
        f"{raw_op!r}. Supported ops: add_column, alter_column, drop_column, "
        "rename_column, add_unique_key, drop_unique_key, add_index, drop_index."
    )


async def apply_table_migration(
    vault_id: uuid.UUID,
    *,
    actor_id: str,
    idempotency_key: str | None,
    operations: list[dict],
) -> dict:
    """Apply a structured table migration atomically and idempotently."""
    key = _validate_idempotency_key(idempotency_key)
    operations = _normalize_migration_operations(operations)
    checksum = table_migration_checksum(operations)

    pool = await get_pool()
    changed_tables: dict[str, dict] = {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            if not vault:
                raise NotFoundError("Vault", str(vault_id))

            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"{vault_id}:{key}",
            )
            existing = await conn.fetchrow(
                """
                SELECT id, applied_at, checksum
                  FROM vault_migrations
                 WHERE vault_id = $1 AND name = $2
                 FOR UPDATE
                """,
                vault_id,
                key,
            )
            if existing:
                if existing["checksum"] != checksum:
                    raise ConflictError(
                        "Table migration idempotency key was already used with "
                        "a different checksum."
                    )
                return {
                    "kind": "table_migration",
                    "vault": vault["name"],
                    "idempotency_key": key,
                    "checksum": checksum,
                    "applied": False,
                    "applied_at": existing["applied_at"].isoformat(),
                    "operations": len(operations),
                    "results": [],
                }

            results: list[dict[str, Any]] = []
            for index, op in enumerate(operations, start=1):
                table_name, kwargs = _migration_op_to_alter_kwargs(op)
                result = await alter_table(
                    vault_id,
                    table_name,
                    actor_id=actor_id,
                    _conn=conn,
                    _defer_index=True,
                    **kwargs,
                )
                results.append({
                    "index": index,
                    "op": op["op"],
                    "table": table_name,
                    "result": result,
                })
                changed_tables[table_name] = result

            row = await conn.fetchrow(
                """
                INSERT INTO vault_migrations (vault_id, name, checksum)
                VALUES ($1, $2, $3)
                RETURNING id, applied_at
                """,
                vault_id,
                key,
                checksum,
            )
            await emit_event(
                conn,
                "table.migration",
                vault_id=vault_id,
                resource_uri=vault_uri(vault["name"]),
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "idempotency_key": key,
                    "checksum": checksum,
                    "operations": len(operations),
                    "tables": sorted(changed_tables),
                },
            )

    async with pool.acquire() as conn:
        for table_name, result in changed_tables.items():
            table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
            if not table:
                continue
            try:
                await index_table_metadata(
                    str(table["id"]),
                    vault_id=vault_id,
                    vault_name=result["vault"],
                    name=table_name,
                    description=table["description"] or "",
                    columns=result["columns"],
                    unique_keys=result.get("unique_keys"),
                    indexes=result.get("indexes"),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("table migration chunk reindex failed for %s: %s", table_name, e)

    return {
        "kind": "table_migration",
        "id": str(row["id"]),
        "vault": vault["name"],
        "idempotency_key": key,
        "checksum": checksum,
        "applied": True,
        "applied_at": row["applied_at"].isoformat(),
        "operations": len(operations),
        "results": results,
    }
