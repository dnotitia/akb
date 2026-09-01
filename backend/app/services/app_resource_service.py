"""Shared app-owned table identity, fingerprint, and mutation guards.

Legacy adoption and structured rollout must agree on what a table baseline is
and on who is allowed to mutate an app-owned table.  This module keeps that
definition independent from either workflow.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
import uuid

import asyncpg

from app.exceptions import ConflictError, ValidationError
from app.repositories import table_data_repo
from app.util.text import to_nfc_any

_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_IDENTIFIER_LENGTH = 63


@dataclass(frozen=True)
class TableOwnershipContext:
    """The only context that may mutate an adopted table."""

    installation_id: uuid.UUID
    app_id: uuid.UUID


def canonical_json(value: Any) -> bytes:
    """Return the repository-wide NFC + sorted-key JSON representation."""

    return json.dumps(
        to_nfc_any(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return list(value) if isinstance(value, list) else []


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _canonical_column(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("table schema columns must be objects")
    result = dict(to_nfc_any(value))
    if set(result) - {
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
    }:
        raise ValidationError("table schema column contains an unsupported field")
    name = result.get("name")
    if (
        not isinstance(name, str)
        or len(name.encode("utf-8")) > _MAX_IDENTIFIER_LENGTH
        or not _TABLE_NAME_RE.fullmatch(name)
    ):
        # Column names are validated by the table service.  Keep the
        # fingerprint boundary strict for rows inserted by older paths too.
        raise ValidationError("table schema column name is invalid")
    result["name"] = name
    for flag in ("required", "unique", "index"):
        if flag in result and not isinstance(result[flag], bool):
            raise ValidationError(f"table schema column {flag} must be boolean")
    result["type"] = table_data_repo.normalize_column_type(result.get("type", "text"))
    for flag in ("required", "unique", "index"):
        if result.get(flag) is False or result.get(flag) is None:
            result.pop(flag, None)
    for key in ("default", "check", "enum", "references", "on_delete"):
        if result.get(key) is None:
            result.pop(key, None)
    # Column-level unique/index flags are shorthand for the declarative
    # metadata below.  Their physical names are generated per vault, so the
    # logical projection records the constraint/index identity once there.
    result.pop("unique", None)
    result.pop("index", None)
    if result.get("references") is not None:
        refs, on_delete = table_data_repo.normalize_reference_spec(
            result["references"], result.get("on_delete")
        )
        result["references"] = refs
        result["on_delete"] = on_delete
    return result


def _canonical_unique_key(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("table schema unique keys must be objects")
    if set(value) - {"name", "columns"}:
        raise ValidationError("table schema unique key contains an unsupported field")
    columns = value.get("columns")
    if not isinstance(columns, list) or not columns or any(
        not isinstance(column, str)
        or len(column.encode("utf-8")) > _MAX_IDENTIFIER_LENGTH
        or not _TABLE_NAME_RE.fullmatch(column)
        for column in columns
    ):
        raise ValidationError("table schema unique-key columns are invalid")
    if len(set(columns)) != len(columns):
        raise ValidationError("table schema unique-key columns must be distinct")
    # Constraint names are physical implementation details: AKB generates
    # them from the vault-qualified table name, so they cannot be part of an
    # app-level desired schema fingerprint.
    return {"columns": list(columns)}


def _canonical_index(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("table schema indexes must be objects")
    if set(value) - {"name", "columns"}:
        raise ValidationError("table schema index contains an unsupported field")
    columns = value.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValidationError("table schema index columns are invalid")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for column in columns:
        raw_name: Any
        raw_order: Any
        if isinstance(column, str):
            raw_name, raw_order = column, "asc"
        elif isinstance(column, dict):
            if set(column) - {"name", "order"}:
                raise ValidationError("table schema index column contains an unsupported field")
            raw_name, raw_order = column.get("name"), column.get("order", "asc")
        else:
            raise ValidationError("table schema index columns are invalid")
        if (
            not isinstance(raw_name, str)
            or len(raw_name.encode("utf-8")) > _MAX_IDENTIFIER_LENGTH
            or not _TABLE_NAME_RE.fullmatch(raw_name)
        ):
            raise ValidationError("table schema index column name is invalid")
        if not isinstance(raw_order, str) or raw_order.lower() not in {"asc", "desc"}:
            raise ValidationError("table schema index column order is invalid")
        name = raw_name
        order = raw_order
        if name in seen:
            raise ValidationError("table schema index columns must be distinct")
        seen.add(name)
        normalized.append({"name": name, "order": order.lower()})
    # As with unique keys, index names vary with the physical vault and are
    # intentionally excluded from the logical app schema.
    return {"columns": normalized}


def canonical_table_descriptor(row: Any) -> dict[str, Any]:
    """Project one table into the logical desired-schema fingerprint shape.

    The table name and ordered column list are semantic. Unique/index names
    are omitted because AKB derives vault-qualified physical names; their
    columns and ordering remain part of the schema contract.
    """
    raw_columns = _json_list(_row_value(row, "columns"))
    columns = [_canonical_column(item) for item in raw_columns]
    columns.sort(key=lambda item: item["name"])
    inline_unique = []
    inline_indexes = []
    for raw_column in raw_columns:
        if not isinstance(raw_column, dict):
            continue
        name = raw_column.get("name")
        if raw_column.get("unique") is True and isinstance(name, str):
            inline_unique.append({"columns": [name]})
        if raw_column.get("index") is True and isinstance(name, str):
            inline_indexes.append({"columns": [{"name": name, "order": "asc"}]})
    unique_keys = [_canonical_unique_key(item) for item in _json_list(_row_value(row, "unique_keys"))]
    unique_identities = {tuple(item["columns"]) for item in unique_keys}
    unique_keys.extend(
        item for item in inline_unique if tuple(item["columns"]) not in unique_identities
    )
    unique_keys.sort(key=canonical_json)
    indexes = [_canonical_index(item) for item in _json_list(_row_value(row, "indexes"))]
    index_identities = {
        tuple((column["name"], column["order"]) for column in item["columns"])
        for item in indexes
    }
    indexes.extend(
        item
        for item in inline_indexes
        if tuple((column["name"], column["order"]) for column in item["columns"])
        not in index_identities
    )
    indexes.sort(key=canonical_json)
    name = _row_value(row, "name")
    if (
        not isinstance(name, str)
        or len(name.encode("utf-8")) > _MAX_IDENTIFIER_LENGTH
        or not _TABLE_NAME_RE.fullmatch(name)
    ):
        raise ValidationError("table schema name is invalid")
    return {
        "name": to_nfc_any(name),
        "columns": columns,
        "unique_keys": unique_keys,
        "indexes": indexes,
    }


def canonical_table_fingerprint(rows: Iterable[Any]) -> str:
    """Hash a sorted collection of table descriptors.

    Only rows supplied by the caller are included.  Callers therefore control
    the allowlist query and cannot accidentally broaden an adoption baseline
    by discovering unrelated tables.
    """

    descriptors = [canonical_table_descriptor(row) for row in rows]
    descriptors.sort(key=lambda item: item["name"])
    return hashlib.sha256(canonical_json(descriptors)).hexdigest()


def normalize_table_allowlist(value: Iterable[str]) -> list[str]:
    """Normalize and validate an explicit table allowlist."""

    if not isinstance(value, list):
        raise ValidationError("table allowlist must be an array")
    values: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValidationError("table allowlist must contain strings")
        name = item.strip()
        if not _TABLE_NAME_RE.fullmatch(name):
            raise ValidationError("table allowlist contains an invalid table name")
        values.add(name)
    if not values:
        raise ValidationError("table allowlist must not be empty")
    return sorted(values)


async def fetch_allowlisted_tables(
    conn: Any,
    vault_id: uuid.UUID,
    table_allowlist: list[str],
    *,
    lock: bool = False,
) -> list[dict[str, Any]]:
    """Read only the table names explicitly present in ``table_allowlist``."""

    rows = await conn.fetch(
        """
        SELECT id, vault_id, name, description, columns, unique_keys, indexes
          FROM vault_tables
         WHERE vault_id = $1 AND name = ANY($2::text[])
         ORDER BY name
        """ + (" FOR SHARE" if lock else ""),
        vault_id,
        table_allowlist,
    )
    return [dict(row) for row in rows]


async def table_ownership(
    conn: Any,
    vault_id: uuid.UUID,
    table_name: str,
) -> dict[str, Any] | None:
    # init.sql deliberately contains only the legacy table surface.  Probe
    # before the ownership query so a real transaction is never aborted by an
    # ``UndefinedTableError`` on a pre-registry database.
    fetchval = getattr(conn, "fetchval", None)
    if fetchval is not None and await fetchval("SELECT to_regclass('public.app_owned_resources')") is None:
        return None
    try:
        row = await conn.fetchrow(
            """
            SELECT resource.installation_id, installation.app_id, resource.status
              FROM app_owned_resources AS resource
              JOIN vault_app_installations AS installation
                ON installation.id = resource.installation_id
             WHERE resource.vault_id = $1
               AND resource.resource_kind = 'table'
               AND resource.resource_key = $2
            """,
            vault_id,
            table_name,
        )
    except asyncpg.UndefinedTableError:
        # A concurrent schema bootstrap may still race the probe.  Preserve
        # the legacy mutation path if the registry disappears between reads.
        return None
    if not row:
        return None
    result = dict(row)
    # The SQL join always supplies these fields.  Keep the shape check so an
    # unrelated registry row can never be treated as ownership.
    if result.get("installation_id") is None or result.get("status") not in {"owned", "retained"}:
        return None
    return result


async def ensure_table_mutation_allowed(
    conn: Any,
    vault_id: uuid.UUID,
    table_name: str,
    *,
    context: TableOwnershipContext | None = None,
) -> None:
    """Reject generic schema mutation of owned or retained app tables.

    A rollout worker may pass the exact app + installation execution context,
    but even that context is valid only for an ``owned`` row in the same Vault.
    """

    ownership = await table_ownership(conn, vault_id, table_name)
    if ownership is None:
        return
    if (
        context is not None
        and ownership["status"] == "owned"
        and ownership["installation_id"] == context.installation_id
        and ownership["app_id"] == context.app_id
    ):
        return
    raise ConflictError("Table is managed by an app installation")


def table_mutation_lock_key(vault_id: uuid.UUID, table_name: str) -> str:
    """Return the transaction lock key shared by adoption and table writes."""

    return f"app-table:{vault_id}:{table_name}"


def app_vault_lock_key(app_id: uuid.UUID, vault_id: uuid.UUID) -> str:
    """Return the lifecycle lock key shared by installation/adoption paths."""

    return f"app-installation:{app_id}:{vault_id}"


async def lock_app_vault_pair(conn: Any, app_id: uuid.UUID, vault_id: uuid.UUID) -> None:
    """Serialize installation lifecycle and legacy adoption for one pair."""

    fetchval = getattr(conn, "fetchval", None)
    if fetchval is None:
        return
    await fetchval(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        app_vault_lock_key(app_id, vault_id),
    )


async def lock_table_mutation(conn: Any, vault_id: uuid.UUID, table_name: str) -> None:
    """Serialize adoption with generic table DDL for one logical table."""

    # A few DB-free service tests pass deliberately minimal connection fakes;
    # real asyncpg connections always expose fetchval.
    fetchval = getattr(conn, "fetchval", None)
    if fetchval is None:
        return
    await fetchval(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        table_mutation_lock_key(vault_id, table_name),
    )


def table_name_is_valid(value: str) -> bool:
    return isinstance(value, str) and _TABLE_NAME_RE.fullmatch(value) is not None
