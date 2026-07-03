"""Merged registry/live schema introspection for vault tables.

Reports the declared registry shape for each table alongside the live
PostgreSQL column types, flagging drift between the two. Extracted from
`table_service`; the canonical-type helper (`_canonical_pg_type`), the
reserved-column set (`_RESERVED`), and the live column-meta reader
(`_fetch_column_meta`) stay in `table_service` because they are shared
with the create/alter paths.
"""

from __future__ import annotations

import uuid

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories import table_data_repo, table_registry_repo
from app.services.table_service import (
    _RESERVED,
    _canonical_pg_type,
    _fetch_column_meta,
)
from app.services.uri_service import table_uri


def _indexed_column_names(indexes: list[dict]) -> set[str]:
    names: set[str] = set()
    for idx in indexes:
        for col in idx.get("columns", []):
            if isinstance(col, dict):
                name = col.get("name")
            else:
                name = col
            if isinstance(name, str):
                names.add(name)
    return names


def _unique_column_names(unique_keys: list[dict]) -> set[str]:
    names: set[str] = set()
    for key in unique_keys:
        cols = key.get("columns", [])
        if len(cols) == 1 and isinstance(cols[0], str):
            names.add(cols[0])
    return names


def _build_table_schema(vault_name: str, table: dict, pg_types: dict[str, str]) -> dict:
    columns = table_registry_repo.parse_columns(table.get("columns"))
    unique_keys = table_registry_repo.parse_json_list(table.get("unique_keys"))
    indexes = table_registry_repo.parse_json_list(table.get("indexes"))
    indexed = _indexed_column_names(indexes)
    unique = _unique_column_names(unique_keys)

    column_items: list[dict] = []
    missing_columns: list[str] = []
    type_mismatches: list[dict] = []
    registry_names = {
        c["name"]
        for c in columns
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    }
    system_columns = sorted(name for name in pg_types if name.lower() in _RESERVED)
    extra_columns = sorted(
        name
        for name in pg_types
        if name not in registry_names and name.lower() not in _RESERVED
    )

    for col in columns:
        name = col["name"]
        logical_type = col.get("type", "text")
        expected_pg_type = _canonical_pg_type(logical_type)
        actual_pg_type = pg_types.get(name)
        drift: dict[str, object] = {
            "missing": actual_pg_type is None,
            "type_mismatch": False,
        }
        if actual_pg_type is None:
            missing_columns.append(name)
        else:
            actual_norm = " ".join(actual_pg_type.lower().split())
            drift["type_mismatch"] = actual_norm != expected_pg_type
            if drift["type_mismatch"]:
                type_mismatches.append({
                    "column": name,
                    "registry_type": logical_type,
                    "expected_pg_type": expected_pg_type,
                    "pg_type": actual_pg_type,
                })

        column_items.append({
            "name": name,
            "type": logical_type,
            "required": bool(col.get("required", False)),
            "default": col.get("default"),
            "check": col.get("check"),
            "enum": col.get("enum"),
            "unique": bool(col.get("unique", False) or name in unique),
            "index": bool(col.get("index", False) or name in indexed),
            "references": col.get("references"),
            "on_delete": col.get("on_delete"),
            "pg_type": actual_pg_type,
            "drift": drift,
        })

    drift_summary = {
        "has_drift": bool(missing_columns or extra_columns or type_mismatches),
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        "type_mismatches": type_mismatches,
    }

    name = table["name"]
    return {
        "kind": "table_schema",
        "uri": table_uri(vault_name, name, collection=table.get("collection")),
        "vault": vault_name,
        "collection": table.get("collection"),
        "name": name,
        "table": name,
        "sql_name": table_data_repo.pg_short_name(name),
        "description": table.get("description"),
        "columns": column_items,
        "unique_keys": unique_keys,
        "indexes": indexes,
        "pg_types": pg_types,
        "system_columns": system_columns,
        "drift": drift_summary,
    }


async def get_table_schema(vault_id: uuid.UUID, table_name: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            raise NotFoundError("Vault", str(vault_id))
        table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
        if not table:
            raise NotFoundError("Table", table_name)
        pg_name = table_data_repo.pg_table_name(vault["name"], table_name)
        pg_types = await _fetch_column_meta(conn, {pg_name})
        return _build_table_schema(vault["name"], table, pg_types)


async def get_vault_schema(vault_id: uuid.UUID) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            raise NotFoundError("Vault", str(vault_id))
        rows = await table_registry_repo.list_for_vault(conn, vault_id)
        tables = []
        for table in rows:
            pg_name = table_data_repo.pg_table_name(vault["name"], table["name"])
            pg_types = await _fetch_column_meta(conn, {pg_name})
            tables.append(_build_table_schema(vault["name"], table, pg_types))
        return {
            "kind": "vault_table_schema",
            "vault": vault["name"],
            "tables": tables,
            "total": len(tables),
        }
