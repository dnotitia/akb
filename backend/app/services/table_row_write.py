"""PostgREST-style row-write compiler for vault tables."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories import table_data_repo, table_registry_repo
from app.services.table_row_query import (
    _add_param,
    _column_meta,
    _compile_ast_filter,
    _compile_filters,
    _compile_select,
    _shape_result,
    _unknown_column,
)
from app.services.user_sql_executor import (
    PermissionDeniedError,
    UniqueViolationError,
    get_user_sql_executor,
)
from app.util.errors import (
    BULK_TOO_LARGE,
    INVALID_ARGUMENT,
    NO_UNIQUE_CONSTRAINT,
    PERMISSION_DENIED,
    SQL_ERROR,
    UNFILTERED_MUTATION,
    UNIQUE_VIOLATION,
    err,
)


MAX_BULK_ROWS = 1000
INSERT_SERVER_CONTROLLED = {"created_by", "updated_at"}
UPDATE_IMMUTABLE = {"id", "created_by", "created_at", "updated_at"}
WRITE_CONTROL_PARAMS = {
    "select",
    "all",
    "on_conflict",
    "resolution",
    "count",
    "order",
    "limit",
    "offset",
}
WRITE_AST_KEYS = {"insert", "update", "delete"}


@dataclass
class RowMutationResponse:
    status_code: int
    body: dict[str, Any] | None
    content_range: str | None = None


@dataclass
class _CompiledMutation:
    sql: str
    params: list[Any]
    fetch: bool
    status_code: int
    projections: list[Any]


@dataclass
class _TableInfo:
    columns: list[dict]
    unique_keys: list[dict]


async def insert_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    actor_id: str,
    body: Any,
    is_admin: bool = False,
    query_params: Sequence[tuple[str, str]] = (),
    prefer_header: str | None = None,
) -> RowMutationResponse | dict[str, Any]:
    loaded = await _load_table(vault_id, table_name)
    if isinstance(loaded, dict):
        return loaded
    compiled = compile_insert_rows(
        vault_name=vault_name,
        table_name=table_name,
        columns=loaded.columns,
        unique_keys=loaded.unique_keys,
        body=body,
        actor_id=actor_id,
        query_params=query_params,
        prefer_header=prefer_header,
    )
    if isinstance(compiled, dict):
        return compiled
    return await _execute_mutation(
        compiled,
        vault_name=vault_name,
        table_name=table_name,
        user_id=user_id,
        is_admin=is_admin,
    )


async def update_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    body: Any,
    is_admin: bool = False,
    query_params: Sequence[tuple[str, str]] = (),
    prefer_header: str | None = None,
) -> RowMutationResponse | dict[str, Any]:
    loaded = await _load_table(vault_id, table_name)
    if isinstance(loaded, dict):
        return loaded
    compiled = compile_update_rows(
        vault_name=vault_name,
        table_name=table_name,
        columns=loaded.columns,
        body=body,
        query_params=query_params,
        prefer_header=prefer_header,
    )
    if isinstance(compiled, dict):
        return compiled
    return await _execute_mutation(
        compiled,
        vault_name=vault_name,
        table_name=table_name,
        user_id=user_id,
        is_admin=is_admin,
    )


async def delete_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    is_admin: bool = False,
    query_params: Sequence[tuple[str, str]] = (),
    prefer_header: str | None = None,
) -> RowMutationResponse | dict[str, Any]:
    loaded = await _load_table(vault_id, table_name)
    if isinstance(loaded, dict):
        return loaded
    compiled = compile_delete_rows(
        vault_name=vault_name,
        table_name=table_name,
        columns=loaded.columns,
        query_params=query_params,
        prefer_header=prefer_header,
    )
    if isinstance(compiled, dict):
        return compiled
    return await _execute_mutation(
        compiled,
        vault_name=vault_name,
        table_name=table_name,
        user_id=user_id,
        is_admin=is_admin,
    )


async def query_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    actor_id: str,
    ast: Mapping[str, Any],
    is_admin: bool = False,
    prefer_header: str | None = None,
) -> RowMutationResponse | dict[str, Any]:
    loaded = await _load_table(vault_id, table_name)
    if isinstance(loaded, dict):
        return loaded
    compiled = compile_ast_mutation(
        vault_name=vault_name,
        table_name=table_name,
        columns=loaded.columns,
        unique_keys=loaded.unique_keys,
        ast=ast,
        actor_id=actor_id,
        prefer_header=prefer_header,
    )
    if isinstance(compiled, dict):
        return compiled
    return await _execute_mutation(
        compiled,
        vault_name=vault_name,
        table_name=table_name,
        user_id=user_id,
        is_admin=is_admin,
    )


def is_write_ast(ast: Mapping[str, Any]) -> bool:
    return any(key in ast for key in WRITE_AST_KEYS)


def compile_insert_rows(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    unique_keys: list[dict] | None = None,
    body: Any,
    actor_id: str,
    query_params: Sequence[tuple[str, str]] = (),
    prefer_header: str | None = None,
) -> _CompiledMutation | dict[str, Any]:
    rows_or_error = _normalize_insert_rows(body)
    if isinstance(rows_or_error, dict):
        return rows_or_error
    rows = rows_or_error
    if len(rows) > MAX_BULK_ROWS:
        return err(
            f"Bulk insert is limited to {MAX_BULK_ROWS} rows.",
            code=BULK_TOO_LARGE,
            max_rows=MAX_BULK_ROWS,
            received_rows=len(rows),
        )

    column_meta = _column_meta(columns)
    params: list[Any] = []
    insert_columns_or_error = _insert_columns(rows, column_meta)
    if isinstance(insert_columns_or_error, dict):
        return insert_columns_or_error
    insert_columns = insert_columns_or_error
    on_conflict_or_error = _compile_on_conflict(
        _last_value(query_params, "on_conflict"),
        column_meta,
        unique_keys or [],
    )
    if isinstance(on_conflict_or_error, dict):
        return on_conflict_or_error
    conflict_columns = on_conflict_or_error

    values_sql: list[str] = []
    for row in rows:
        cells: list[str] = []
        for col in insert_columns:
            if col == "created_by":
                cells.append(_add_param(params, actor_id))
            elif col in row:
                cells.append(_add_param(params, _normalize_value(row[col], column_meta[col])))
            else:
                cells.append("DEFAULT")
        values_sql.append(f"({', '.join(cells)})")

    conflict_sql = ""
    if conflict_columns:
        conflict_sql = _compile_upsert_clause(
            conflict_columns=conflict_columns,
            insert_columns=insert_columns,
            prefer_header=prefer_header,
        )

    fetch = _prefer_return_representation(prefer_header)
    projections: list[Any] = []
    returning_sql = ""
    if fetch:
        returning_or_error = _compile_returning(_last_value(query_params, "select"), column_meta, params)
        if isinstance(returning_or_error, dict):
            return returning_or_error
        returning_sql, projections = returning_or_error

    sql = (
        f"INSERT INTO {table_data_repo.pg_table_name(vault_name, table_name)} "
        f"({', '.join(table_data_repo.safe_ident(c) for c in insert_columns)}) "
        f"VALUES {', '.join(values_sql)}{conflict_sql}{returning_sql}"
    )
    return _CompiledMutation(
        sql=sql,
        params=params,
        fetch=fetch,
        status_code=201 if fetch else 204,
        projections=projections,
    )


def compile_ast_mutation(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    unique_keys: list[dict] | None = None,
    ast: Mapping[str, Any],
    actor_id: str,
    prefer_header: str | None = None,
) -> _CompiledMutation | dict[str, Any]:
    keys = [key for key in WRITE_AST_KEYS if key in ast]
    if len(keys) != 1:
        return err("Write AST must include exactly one of insert, update, or delete.", code=INVALID_ARGUMENT)
    key = keys[0]
    returning = _ast_returning_select(ast)
    if isinstance(returning, dict):
        return returning
    ast_prefer = _ast_prefer_header(ast, prefer_header)
    if key == "insert":
        query_params = []
        if returning is not None:
            query_params.append(("select", returning))
        on_conflict = ast.get("on_conflict")
        if on_conflict is not None:
            if not isinstance(on_conflict, str):
                return err("AST on_conflict must be a string.", code=INVALID_ARGUMENT)
            query_params.append(("on_conflict", on_conflict))
        return compile_insert_rows(
            vault_name=vault_name,
            table_name=table_name,
            columns=columns,
            unique_keys=unique_keys,
            body=ast["insert"],
            actor_id=actor_id,
            query_params=query_params,
            prefer_header=ast_prefer,
        )
    if key == "update":
        return _compile_update_ast(
            vault_name=vault_name,
            table_name=table_name,
            columns=columns,
            body=ast["update"],
            ast=ast,
            returning=returning,
            prefer_header=ast_prefer,
        )
    delete_value = ast["delete"]
    if delete_value is not True:
        return err("AST delete must be true.", code=INVALID_ARGUMENT)
    return _compile_delete_ast(
        vault_name=vault_name,
        table_name=table_name,
        columns=columns,
        ast=ast,
        returning=returning,
        prefer_header=ast_prefer,
    )


def compile_update_rows(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    body: Any,
    query_params: Sequence[tuple[str, str]] = (),
    prefer_header: str | None = None,
) -> _CompiledMutation | dict[str, Any]:
    if not isinstance(body, Mapping):
        return err("PATCH /rows expects a JSON object body.", code=INVALID_ARGUMENT)
    column_meta = _column_meta(columns)
    params: list[Any] = []
    set_parts = _compile_update_set_parts(body, column_meta, params)
    if isinstance(set_parts, dict):
        return set_parts
    set_parts.append("updated_at = NOW()")

    where_or_error = _compile_mutation_where(query_params, column_meta, params)
    if isinstance(where_or_error, dict):
        return where_or_error
    where_sql = where_or_error

    fetch = _prefer_return_representation(prefer_header)
    projections: list[Any] = []
    returning_sql = ""
    if fetch:
        returning_or_error = _compile_returning(_last_value(query_params, "select"), column_meta, params)
        if isinstance(returning_or_error, dict):
            return returning_or_error
        returning_sql, projections = returning_or_error

    sql = (
        f"UPDATE {table_data_repo.pg_table_name(vault_name, table_name)} "
        f"SET {', '.join(set_parts)} WHERE {where_sql}{returning_sql}"
    )
    return _CompiledMutation(
        sql=sql,
        params=params,
        fetch=fetch,
        status_code=200 if fetch else 204,
        projections=projections,
    )


def _compile_update_ast(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    body: Any,
    ast: Mapping[str, Any],
    returning: str | None,
    prefer_header: str | None,
) -> _CompiledMutation | dict[str, Any]:
    if not isinstance(body, Mapping):
        return err("AST update must be an object.", code=INVALID_ARGUMENT)
    column_meta = _column_meta(columns)
    params: list[Any] = []
    set_parts = _compile_update_set_parts(body, column_meta, params)
    if isinstance(set_parts, dict):
        return set_parts
    set_parts.append("updated_at = NOW()")
    where_or_error = _compile_ast_mutation_where(ast, column_meta, params)
    if isinstance(where_or_error, dict):
        return where_or_error
    fetch = _prefer_return_representation(prefer_header)
    projections: list[Any] = []
    returning_sql = ""
    if fetch:
        returning_or_error = _compile_returning(returning, column_meta, params)
        if isinstance(returning_or_error, dict):
            return returning_or_error
        returning_sql, projections = returning_or_error
    sql = (
        f"UPDATE {table_data_repo.pg_table_name(vault_name, table_name)} "
        f"SET {', '.join(set_parts)} WHERE {where_or_error}{returning_sql}"
    )
    return _CompiledMutation(
        sql=sql,
        params=params,
        fetch=fetch,
        status_code=200 if fetch else 204,
        projections=projections,
    )


def compile_delete_rows(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    query_params: Sequence[tuple[str, str]] = (),
    prefer_header: str | None = None,
) -> _CompiledMutation | dict[str, Any]:
    column_meta = _column_meta(columns)
    params: list[Any] = []
    where_or_error = _compile_mutation_where(query_params, column_meta, params)
    if isinstance(where_or_error, dict):
        return where_or_error
    where_sql = where_or_error

    fetch = _prefer_return_representation(prefer_header)
    projections: list[Any] = []
    returning_sql = ""
    if fetch:
        returning_or_error = _compile_returning(_last_value(query_params, "select"), column_meta, params)
        if isinstance(returning_or_error, dict):
            return returning_or_error
        returning_sql, projections = returning_or_error

    sql = f"DELETE FROM {table_data_repo.pg_table_name(vault_name, table_name)} WHERE {where_sql}{returning_sql}"
    return _CompiledMutation(
        sql=sql,
        params=params,
        fetch=fetch,
        status_code=200 if fetch else 204,
        projections=projections,
    )


def _compile_delete_ast(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    ast: Mapping[str, Any],
    returning: str | None,
    prefer_header: str | None,
) -> _CompiledMutation | dict[str, Any]:
    column_meta = _column_meta(columns)
    params: list[Any] = []
    where_or_error = _compile_ast_mutation_where(ast, column_meta, params)
    if isinstance(where_or_error, dict):
        return where_or_error
    fetch = _prefer_return_representation(prefer_header)
    projections: list[Any] = []
    returning_sql = ""
    if fetch:
        returning_or_error = _compile_returning(returning, column_meta, params)
        if isinstance(returning_or_error, dict):
            return returning_or_error
        returning_sql, projections = returning_or_error
    sql = f"DELETE FROM {table_data_repo.pg_table_name(vault_name, table_name)} WHERE {where_or_error}{returning_sql}"
    return _CompiledMutation(
        sql=sql,
        params=params,
        fetch=fetch,
        status_code=200 if fetch else 204,
        projections=projections,
    )


async def _load_table(vault_id: uuid.UUID, table_name: str) -> _TableInfo | dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
        if not table:
            raise NotFoundError("Table", table_name)
        return _TableInfo(
            columns=table_registry_repo.parse_columns(table["columns"]),
            unique_keys=table_registry_repo.parse_json_list(table.get("unique_keys")),
        )


async def _execute_mutation(
    compiled: _CompiledMutation,
    *,
    vault_name: str,
    table_name: str,
    user_id: uuid.UUID | str,
    is_admin: bool,
) -> RowMutationResponse | dict[str, Any]:
    try:
        result = await get_user_sql_executor().execute(
            user_id=user_id,
            sql=compiled.sql,
            params=compiled.params,
            fetch=compiled.fetch,
            is_admin=is_admin,
            vault_names=[vault_name],
        )
    except PermissionDeniedError as e:
        return err(str(e), code=PERMISSION_DENIED, pg_sqlstate=e.pg_sqlstate)
    except UniqueViolationError as e:
        return err(str(e), code=UNIQUE_VIOLATION, pg_sqlstate=e.pg_sqlstate)
    except asyncpg.PostgresError as e:
        return err(str(e), code=SQL_ERROR, pg_sqlstate=getattr(e, "sqlstate", None))

    if compiled.fetch:
        body, _unused = _shape_result(
            result,
            vault_name=vault_name,
            table_name=table_name,
            projections=compiled.projections,
            count_exact=False,
            offset=0,
        )
        total = len(body["items"])
        content_range = f"0-{total - 1}/{total}" if total else "*/0"
        body["total"] = total
        return RowMutationResponse(
            status_code=compiled.status_code,
            body=body,
            content_range=content_range,
        )

    affected_rows = int(result.get("affected_rows") or 0)
    return RowMutationResponse(
        status_code=compiled.status_code,
        body=None,
        content_range=f"*/{affected_rows}",
    )


def _normalize_insert_rows(body: Any) -> list[Mapping[str, Any]] | dict[str, Any]:
    rows = body if isinstance(body, list) else [body]
    if not rows:
        return err("POST /rows expects at least one row.", code=INVALID_ARGUMENT)
    out: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return err("POST /rows expects a JSON object or array of objects.", code=INVALID_ARGUMENT)
        out.append(row)
    return out


def _insert_columns(
    rows: Sequence[Mapping[str, Any]],
    column_meta: dict[str, str],
) -> list[str] | dict[str, Any]:
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for raw_col in row:
            if not isinstance(raw_col, str) or not raw_col:
                return err("INSERT column names must be non-empty strings.", code=INVALID_ARGUMENT)
            if raw_col in INSERT_SERVER_CONTROLLED:
                continue
            if raw_col not in column_meta:
                return _unknown_column(raw_col, column_meta)
            if raw_col not in seen:
                ordered.append(raw_col)
                seen.add(raw_col)
    ordered.append("created_by")
    return ordered


def _compile_update_set_parts(
    body: Mapping[str, Any],
    column_meta: dict[str, str],
    params: list[Any],
) -> list[str] | dict[str, Any]:
    set_parts: list[str] = []
    for raw_col, value in body.items():
        if not isinstance(raw_col, str) or not raw_col:
            return err("PATCH column names must be non-empty strings.", code=INVALID_ARGUMENT)
        if raw_col in UPDATE_IMMUTABLE:
            continue
        if raw_col not in column_meta:
            return _unknown_column(raw_col, column_meta)
        set_parts.append(
            f"{table_data_repo.safe_ident(raw_col)} = "
            f"{_add_param(params, _normalize_value(value, column_meta[raw_col]))}"
        )
    if not set_parts:
        return err("PATCH body must include at least one mutable column.", code=INVALID_ARGUMENT)
    return set_parts


def _compile_mutation_where(
    query_params: Sequence[tuple[str, str]],
    column_meta: dict[str, str],
    params: list[Any],
) -> str | dict[str, Any]:
    filter_params = [
        (key, value)
        for key, value in query_params
        if key not in WRITE_CONTROL_PARAMS
    ]
    if not filter_params and not _all_rows_enabled(query_params):
        return err(
            "PATCH and DELETE require a filter unless all=true is explicit.",
            code=UNFILTERED_MUTATION,
        )
    where_or_error = _compile_filters(filter_params, column_meta, params)
    if isinstance(where_or_error, dict):
        return where_or_error
    return where_or_error or "TRUE"


def _compile_ast_mutation_where(
    ast: Mapping[str, Any],
    column_meta: dict[str, str],
    params: list[Any],
) -> str | dict[str, Any]:
    node = None
    for key in ("where", "filter"):
        if key in ast:
            node = ast[key]
            break
    if node is None and any(key in ast for key in ("and", "or", "col", "jsonb")):
        node = ast
    if node is None:
        if _ast_all_rows_enabled(ast):
            return "TRUE"
        return err(
            "PATCH and DELETE require a filter unless all=true is explicit.",
            code=UNFILTERED_MUTATION,
        )
    where_or_error = _compile_ast_filter(node, column_meta, params, depth=1)
    if isinstance(where_or_error, dict):
        return where_or_error
    return where_or_error or "TRUE"


def _compile_returning(
    select_value: str | None,
    column_meta: dict[str, str],
    params: list[Any],
) -> tuple[str, list[Any]] | dict[str, Any]:
    projections_or_error = _compile_select(select_value, column_meta, params)
    if isinstance(projections_or_error, dict):
        return projections_or_error
    return f" RETURNING {', '.join(p.sql for p in projections_or_error)}", projections_or_error


def _ast_returning_select(ast: Mapping[str, Any]) -> str | None | dict[str, Any]:
    value = ast.get("returning", ast.get("select"))
    if value is None:
        return None
    if value is True:
        return "*"
    if value is False:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return err("AST returning entries must be strings.", code=INVALID_ARGUMENT)
            out.append(item)
        return ",".join(out)
    return err("AST returning must be a string, string array, or boolean.", code=INVALID_ARGUMENT)


def _ast_prefer_header(ast: Mapping[str, Any], prefer_header: str | None) -> str | None:
    parts = [prefer_header] if prefer_header else []
    if ast.get("returning") is not None or ast.get("select") is not None:
        parts.append("return=representation")
    resolution = ast.get("resolution")
    if resolution is not None:
        if not isinstance(resolution, str):
            return prefer_header
        parts.append(f"resolution={resolution}")
    return ", ".join(parts) if parts else None


def _compile_on_conflict(
    raw: str | None,
    column_meta: dict[str, str],
    unique_keys: list[dict],
) -> list[str] | dict[str, Any]:
    if raw is None or not raw.strip():
        return []
    columns = [part.strip() for part in raw.split(",") if part.strip()]
    if not columns:
        return err("on_conflict must name at least one column.", code=INVALID_ARGUMENT)
    seen: set[str] = set()
    for col in columns:
        if col not in column_meta:
            return _unknown_column(col, column_meta)
        key = col.lower()
        if key in seen:
            return err("on_conflict columns must be distinct.", code=INVALID_ARGUMENT)
        seen.add(key)
    if _is_unique_conflict_target(columns, unique_keys):
        return columns
    return err(
        "on_conflict must target an existing UNIQUE or PRIMARY KEY constraint.",
        code=NO_UNIQUE_CONSTRAINT,
        target=columns,
    )


def _is_unique_conflict_target(columns: list[str], unique_keys: list[dict]) -> bool:
    lowered = {col.lower() for col in columns}
    if lowered == {"id"}:
        return True
    for unique_key in unique_keys:
        raw_cols = unique_key.get("columns") if isinstance(unique_key, dict) else None
        if not isinstance(raw_cols, list) or len(raw_cols) != len(columns):
            continue
        if {str(col).lower() for col in raw_cols} == lowered:
            return True
    return False


def _compile_upsert_clause(
    *,
    conflict_columns: list[str],
    insert_columns: list[str],
    prefer_header: str | None,
) -> str:
    target = ", ".join(table_data_repo.safe_ident(col) for col in conflict_columns)
    if _prefer_ignore_duplicates(prefer_header):
        return f" ON CONFLICT ({target}) DO NOTHING"
    conflict_lookup = {col.lower() for col in conflict_columns}
    set_parts = [
        f"{table_data_repo.safe_ident(col)} = EXCLUDED.{table_data_repo.safe_ident(col)}"
        for col in insert_columns
        if col.lower() not in conflict_lookup
        and col not in {"created_by", *UPDATE_IMMUTABLE}
    ]
    set_parts.append("updated_at = NOW()")
    return f" ON CONFLICT ({target}) DO UPDATE SET {', '.join(set_parts)}"


def _prefer_return_representation(prefer_header: str | None) -> bool:
    return bool(prefer_header and "return=representation" in prefer_header.lower())


def _prefer_ignore_duplicates(prefer_header: str | None) -> bool:
    return bool(prefer_header and "resolution=ignore-duplicates" in prefer_header.lower())


def _all_rows_enabled(query_params: Sequence[tuple[str, str]]) -> bool:
    value = _last_value(query_params, "all")
    return bool(value and value.lower() in {"1", "true", "yes"})


def _ast_all_rows_enabled(ast: Mapping[str, Any]) -> bool:
    value = ast.get("all")
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.lower() in {"1", "true", "yes"}


def _last_value(query_params: Sequence[tuple[str, str]], key: str) -> str | None:
    values = [v for k, v in query_params if k == key]
    return values[-1] if values else None


def _normalize_value(value: Any, type_name: str) -> Any:
    if type_name == "json" and not isinstance(value, str):
        return json.dumps(value)
    return value
