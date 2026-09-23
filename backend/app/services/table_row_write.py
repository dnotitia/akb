"""PostgREST-style row-write compiler for vault tables."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories import table_data_repo, table_registry_repo
from app.services.row_query_ast import _compile_ast_filter
from app.services.row_query_base import (
    _ColumnMeta,
    _ColumnRef,
    _add_param,
    _column_meta,
    _compile_select,
    _unknown_column,
)
from app.services.row_query_shape import _shape_result
from app.services.row_query_string import _compile_filters, _is_filter_value
from app.services.user_sql_executor import (
    PermissionDeniedError,
    UniqueViolationError,
    get_user_sql_executor,
)
from app.util.errors import (
    BULK_TOO_LARGE,
    CONFLICT,
    INVALID_ARGUMENT,
    NO_UNIQUE_CONSTRAINT,
    PERMISSION_DENIED,
    SQL_ERROR,
    UNFILTERED_MUTATION,
    UNIQUE_VIOLATION,
    err,
)


def _is_json_type(type_name: str) -> bool:
    return type_name in {"json", "jsonb"}


MAX_BULK_ROWS = 1000
INSERT_SERVER_CONTROLLED = {"created_by", "updated_at"}
UPDATE_IMMUTABLE = {"id", "created_by", "created_at", "updated_at", "row_commit"}
# Row-CAS token (migration 108 + create-time DDL): server-minted, never
# user-writable. Callers pin it via the `expected_row_commit` control
# param (query-string or AST `cas` key), matched as an extra WHERE
# conjunct; zero matched rows → 409, never a silent no-op.
ROW_COMMIT_COLUMN = "row_commit"
EXPECTED_ROW_COMMIT_PARAM = "expected_row_commit"
WRITE_CONTROL_PARAMS = {
    "select",
    "all",
    "on_conflict",
    "resolution",
    "count",
    "order",
    "limit",
    "offset",
    EXPECTED_ROW_COMMIT_PARAM,
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
    # Row-CAS guard: True when the compiler pinned an expected_row_commit
    # conjunct. The executor maps zero-matched-rows to 409 ONLY on this
    # flag — never by sniffing the SQL text (a refactor of the conjunct
    # spelling must not silently turn a 409 into a success no-op).
    cas_guarded: bool = False


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
        actor_id=actor_id,
        is_admin=is_admin,
    )


async def update_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    body: Any,
    actor_id: str,
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
        actor_id=actor_id,
        is_admin=is_admin,
    )


async def delete_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    is_admin: bool = False,
    actor_id: str,
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
        actor_id=actor_id,
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
        actor_id=actor_id,
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
    resolved_or_error = _insert_columns(rows, column_meta)
    if isinstance(resolved_or_error, dict):
        return resolved_or_error
    insert_columns, resolved_rows = resolved_or_error
    on_conflict_or_error = _compile_on_conflict(
        _last_value(query_params, "on_conflict"),
        column_meta,
        unique_keys or [],
    )
    if isinstance(on_conflict_or_error, dict):
        return on_conflict_or_error
    conflict_columns = on_conflict_or_error

    values_sql: list[str] = []
    for row in resolved_rows:
        cells: list[str] = []
        for col in insert_columns:
            if col.name == "created_by":
                cells.append(_add_param(params, actor_id))
            elif col.pg_name in row:
                cells.append(_add_param(params, _normalize_value(row[col.pg_name], col.type_name)))
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
        f"({', '.join(c.pg_name for c in insert_columns)}) "
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
        query_params: list[tuple[str, Any]] = []
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

    # Row CAS is opt-in. A caller that sends `expected_row_commit` pins the
    # mutation to the row it observed: a stale token matches nothing and
    # surfaces as 409 (see `_execute_mutation`'s affected_rows check) instead
    # of silently winning a lost update. A caller that sends none gets the
    # unguarded mutation this endpoint has always performed — its own filter,
    # unchanged, with last-write-wins semantics.
    #
    # Requiring the token instead would be the stronger contract, and it is
    # where this should end up. It cannot start there: the first-party web UI
    # and the generated client both call PATCH/DELETE without a token, so
    # requiring it makes row editing return 400 for every existing caller. The
    # order is UI first, then the client, then this. Flipping it back is
    # re-adding a rejection here once nothing reaches it without a token.
    #
    # NOTE (forgeability): per-user roles hold table-level UPDATE (no column
    # REVOKEs), so a caller CAN set row_commit via raw akb_sql today. That
    # writes a token nobody else holds (the trigger overwrites it on the next
    # UPDATE anyway) — it can only deny oneself, never forge another writer's
    # match. Compiled paths (REST/AST) reject row_commit outright (reserved +
    # immutable sets above).
    cas_or_error = _extract_expected_row_commit(query_params)
    if isinstance(cas_or_error, dict):
        return cas_or_error
    cas_guarded = cas_or_error is not None
    if cas_guarded:
        where_sql = f"({where_sql}) AND row_commit = {_add_param(params, cas_or_error)}"

    fetch = _prefer_return_representation(prefer_header)
    projections: list[Any] = []
    returning_sql = ""
    if fetch:
        returning_or_error = _compile_returning(
            _control_value(query_params, column_meta, "select"), column_meta, params,
        )
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
        cas_guarded=cas_guarded,
    )


def _compile_update_ast(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    body: Any,
    ast: Mapping[str, Any],
    returning: str | list[str] | None,
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
    # Opt-in, as on the REST paths.
    cas_token = ast.get("cas", ast.get("expected_row_commit"))
    cas_guarded = isinstance(cas_token, str) and bool(cas_token)
    if cas_guarded:
        where_or_error = f"({where_or_error}) AND row_commit = {_add_param(params, cas_token)}"
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
        cas_guarded=cas_guarded,
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

    # Opt-in, as on the UPDATE path above.
    cas_or_error = _extract_expected_row_commit(query_params)
    if isinstance(cas_or_error, dict):
        return cas_or_error
    cas_guarded = cas_or_error is not None
    if cas_guarded:
        where_sql = f"({where_sql}) AND row_commit = {_add_param(params, cas_or_error)}"

    fetch = _prefer_return_representation(prefer_header)
    projections: list[Any] = []
    returning_sql = ""
    if fetch:
        returning_or_error = _compile_returning(
            _control_value(query_params, column_meta, "select"), column_meta, params,
        )
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
        cas_guarded=cas_guarded,
    )


def _compile_delete_ast(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    ast: Mapping[str, Any],
    returning: str | list[str] | None,
    prefer_header: str | None,
) -> _CompiledMutation | dict[str, Any]:
    column_meta = _column_meta(columns)
    params: list[Any] = []
    where_or_error = _compile_ast_mutation_where(ast, column_meta, params)
    if isinstance(where_or_error, dict):
        return where_or_error
    # Opt-in, as on the REST paths.
    cas_token = ast.get("cas", ast.get("expected_row_commit"))
    cas_guarded = isinstance(cas_token, str) and bool(cas_token)
    if cas_guarded:
        where_or_error = f"({where_or_error}) AND row_commit = {_add_param(params, cas_token)}"
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
        cas_guarded=cas_guarded,
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
    actor_id: str,
) -> RowMutationResponse | dict[str, Any]:
    try:
        result = await get_user_sql_executor().execute(
            user_id=user_id,
            actor_id=actor_id,
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
        if total == 0 and compiled.cas_guarded:
            # CAS-guarded mutation matched nothing: the row moved under
            # the caller (stale token) or never existed under this
            # filter. Either way the caller must re-read — never report
            # a silent no-op as success.
            return err(
                "row_commit moved: re-read the row and retry with its "
                "current row_commit.",
                code=CONFLICT,
                hint="GET the row, take its fresh row_commit, retry the mutation.",
            )
        content_range = f"0-{total - 1}/{total}" if total else "*/0"
        body["total"] = total
        return RowMutationResponse(
            status_code=compiled.status_code,
            body=body,
            content_range=content_range,
        )

    affected_rows = int(result.get("affected_rows") or 0)
    if affected_rows == 0 and compiled.cas_guarded:
        return err(
            "row_commit moved: re-read the row and retry with its "
            "current row_commit.",
            code=CONFLICT,
            hint="GET the row, take its fresh row_commit, retry the mutation.",
        )
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
    column_meta: _ColumnMeta,
) -> tuple[list[_ColumnRef], list[dict[str, Any]]] | dict[str, Any]:
    """Resolve each row's keys (logical names) to columns.

    Returns the ordered union of columns plus `created_by`, and each row
    re-keyed by physical name. Rows may spell a column differently; two keys
    in one row that name the same column are refused rather than letting
    one silently win."""
    ordered: list[_ColumnRef] = []
    seen: set[str] = set()
    resolved_rows: list[dict[str, Any]] = []
    for row in rows:
        resolved: dict[str, Any] = {}
        for raw_col, value in row.items():
            if not isinstance(raw_col, str) or not raw_col:
                return err("INSERT column names must be non-empty strings.", code=INVALID_ARGUMENT)
            col = column_meta.resolve(raw_col)
            if col is None:
                return _unknown_column(raw_col, column_meta)
            if col.name in INSERT_SERVER_CONTROLLED:
                continue
            if col.pg_name in resolved:
                return err(
                    f"INSERT row names column {col.name!r} more than once.",
                    code=INVALID_ARGUMENT,
                )
            resolved[col.pg_name] = value
            if col.pg_name not in seen:
                ordered.append(col)
                seen.add(col.pg_name)
        resolved_rows.append(resolved)
    ordered.append(_ColumnRef("created_by", "created_by", "text"))
    return ordered, resolved_rows


def _compile_update_set_parts(
    body: Mapping[str, Any],
    column_meta: _ColumnMeta,
    params: list[Any],
) -> list[str] | dict[str, Any]:
    set_parts: list[str] = []
    seen: set[str] = set()
    for raw_col, value in body.items():
        if not isinstance(raw_col, str) or not raw_col:
            return err("PATCH column names must be non-empty strings.", code=INVALID_ARGUMENT)
        if raw_col in UPDATE_IMMUTABLE:
            continue
        col = column_meta.resolve(raw_col)
        if col is None:
            return _unknown_column(raw_col, column_meta)
        if col.name in UPDATE_IMMUTABLE:
            continue
        if col.pg_name in seen:
            return err(
                f"PATCH body names column {col.name!r} more than once.",
                code=INVALID_ARGUMENT,
            )
        seen.add(col.pg_name)
        set_parts.append(
            f"{col.pg_name} = {_add_param(params, _normalize_value(value, col.type_name))}"
        )
    if not set_parts:
        return err("PATCH body must include at least one mutable column.", code=INVALID_ARGUMENT)
    return set_parts


def _extract_expected_row_commit(
    query_params: Sequence[tuple[str, str]],
) -> str | None | dict[str, Any]:
    """Pull the CAS token out of the control params (not a column filter).

    Returns the token string, None when absent, or an err dict when the
    shape is wrong (empty value / repeated param). The token is matched
    with plain `eq.` semantics — equality only, never ordering — mirroring
    Document `expected_commit`. A column literally named like the param is
    impossible (`row_commit` is reserved), so no column-identity conflict.
    """
    seen: list[str] = []
    for key, value in query_params:
        if key == EXPECTED_ROW_COMMIT_PARAM:
            seen.append(value)
    if not seen:
        return None
    if len(seen) > 1:
        return err(
            "expected_row_commit must appear at most once.",
            code=INVALID_ARGUMENT,
        )
    token = seen[0]
    if token.startswith("eq."):
        token = token[3:]
    if not token:
        return err(
            "expected_row_commit must be a non-empty token.",
            code=INVALID_ARGUMENT,
        )
    return token


# The values `all` takes as a control; `_all_rows_enabled` reads the first three.
_ALL_ROWS_VALUES = {"1", "true", "yes", "0", "false", "no"}


def _mutation_filter_key(key: str, value: str, column_meta: _ColumnMeta) -> bool:
    """Whether a PATCH/DELETE query-string key is a filter.

    A column can share a name with a control param (a `count` or `Order`
    column, a `Select` header — #433 resolves names case-insensitively). An
    UPDATE/DELETE that looks filtered must never quietly become broader than
    the caller intended (8d04a2aa), so a key naming a column is a filter
    unless it is a control this mutation reads AND its value is one that
    control takes: `select` a column list (anything but `<op>.<value>`),
    `all` a yes/no, `expected_row_commit` any token — it is always the CAS
    token. Everything else a mutation never reads, so on a column it can
    only be a filter, and a malformed one is refused, not ignored.
    """
    if key not in WRITE_CONTROL_PARAMS:
        return True
    if key == EXPECTED_ROW_COMMIT_PARAM or key not in column_meta:
        return False
    if key == "select":
        return _is_filter_value(value)
    if key == "all":
        return value.lower() not in _ALL_ROWS_VALUES
    return True


def _control_value(
    query_params: Sequence[tuple[str, str]], column_meta: _ColumnMeta, key: str,
) -> str | None:
    """The last value of control `key` that is not a filter on a column."""
    values = [
        v for k, v in query_params
        if k == key and not _mutation_filter_key(k, v, column_meta)
    ]
    return values[-1] if values else None


def _compile_mutation_where(
    query_params: Sequence[tuple[str, str]],
    column_meta: _ColumnMeta,
    params: list[Any],
) -> str | dict[str, Any]:
    filter_params = [
        (key, value)
        for key, value in query_params
        if _mutation_filter_key(key, value, column_meta)
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
    column_meta: _ColumnMeta,
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
    select_value: str | Sequence[str] | None,
    column_meta: _ColumnMeta,
    params: list[Any],
) -> tuple[str, list[Any]] | dict[str, Any]:
    projections_or_error = _compile_select(select_value, column_meta, params)
    if isinstance(projections_or_error, dict):
        return projections_or_error
    return f" RETURNING {', '.join(p.sql for p in projections_or_error)}", projections_or_error


def _ast_returning_select(ast: Mapping[str, Any]) -> str | list[str] | None | dict[str, Any]:
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
        # Kept a list: re-joining on "," would split a header containing one.
        return out
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
    column_meta: _ColumnMeta,
    unique_keys: list[dict],
) -> list[_ColumnRef] | dict[str, Any]:
    if raw is None or not raw.strip():
        return []
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        return err("on_conflict must name at least one column.", code=INVALID_ARGUMENT)
    columns: list[_ColumnRef] = []
    for name in names:
        col = column_meta.resolve(name)
        if col is None:
            return _unknown_column(name, column_meta)
        if col in columns:
            return err("on_conflict columns must be distinct.", code=INVALID_ARGUMENT)
        columns.append(col)
    if _is_unique_conflict_target(columns, unique_keys):
        return columns
    return err(
        "on_conflict must target an existing UNIQUE or PRIMARY KEY constraint.",
        code=NO_UNIQUE_CONSTRAINT,
        target=[col.name for col in columns],
    )


def _is_unique_conflict_target(columns: list[_ColumnRef], unique_keys: list[dict]) -> bool:
    # Declared keys record logical names; compare through the same key.
    wanted = {table_data_repo.column_key(col.name) for col in columns}
    if wanted == {"id"}:
        return True
    for unique_key in unique_keys:
        raw_cols = unique_key.get("columns") if isinstance(unique_key, dict) else None
        if not isinstance(raw_cols, list) or len(raw_cols) != len(columns):
            continue
        if {table_data_repo.column_key(str(col)) for col in raw_cols} == wanted:
            return True
    return False


def _compile_upsert_clause(
    *,
    conflict_columns: list[_ColumnRef],
    insert_columns: list[_ColumnRef],
    prefer_header: str | None,
) -> str:
    target = ", ".join(col.pg_name for col in conflict_columns)
    if _prefer_ignore_duplicates(prefer_header):
        return f" ON CONFLICT ({target}) DO NOTHING"
    conflicting = {col.pg_name for col in conflict_columns}
    set_parts = [
        f"{col.pg_name} = EXCLUDED.{col.pg_name}"
        for col in insert_columns
        if col.pg_name not in conflicting
        and col.name not in {"created_by", *UPDATE_IMMUTABLE}
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
    if _is_json_type(type_name) and not isinstance(value, str):
        return json.dumps(value)
    # JSON decoders represent decimal literals as binary floats. Passing that
    # float directly to PostgreSQL NUMERIC preserves the binary approximation
    # (for example 0.87 becomes a long 0.86999… value). Convert through the
    # shortest decimal string so structured row writes keep the value the user
    # actually entered. Booleans are intentionally excluded: PG should reject
    # them as a type mismatch rather than silently treating True as 1.
    if (
        type_name in {"numeric", "number"}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return Decimal(str(value))
    return value
