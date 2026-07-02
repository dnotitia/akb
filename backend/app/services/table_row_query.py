"""PostgREST-style row-read compiler for vault tables."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories import table_data_repo, table_registry_repo
from app.services.user_sql_executor import PermissionDeniedError, get_user_sql_executor
from app.util.errors import PERMISSION_DENIED, SQL_ERROR, UNDEFINED_COLUMN, err
from app.util.text import fuzzy_hint


DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_BOOL_DEPTH = 3
BOOKKEEPING_COLUMNS = {
    "id": "uuid",
    "created_by": "text",
    "created_at": "timestamp",
    "updated_at": "timestamp",
}
CAST_SQL = {
    "int": "integer",
    "numeric": "numeric",
    "float": "double precision",
    "bool": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "uuid": "uuid",
    "text": "text",
}
_JSON_PATH_RE = re.compile(
    r"^(?P<base>[a-z][a-z0-9_]*)(?:(?P<arrow>->>|#>>)(?P<path>[^:]+))?(?:::(?P<cast>[a-z]+))?$"
)


@dataclass
class RowQueryResponse:
    body: dict[str, Any]
    content_range: str | None = None


@dataclass
class _Operand:
    sql: str
    params: list[Any]
    type_name: str


@dataclass
class _Projection:
    sql: str
    output_key: str
    result_key: str


@dataclass
class _Page:
    limit: int
    offset: int


async def select_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    is_admin: bool = False,
    query_params: Sequence[tuple[str, str]] = (),
    range_header: str | None = None,
    prefer_header: str | None = None,
) -> RowQueryResponse | dict[str, Any]:
    """Compile URL query params into one parameterized SELECT and execute it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            raise NotFoundError("Vault", str(vault_id))
        table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
        if not table:
            raise NotFoundError("Table", table_name)
        columns = table_registry_repo.parse_columns(table["columns"])

    compiled_or_error = compile_row_query(
        vault_name=vault_name,
        table_name=table_name,
        columns=columns,
        query_params=query_params,
        range_header=range_header,
        prefer_header=prefer_header,
    )
    if isinstance(compiled_or_error, dict) and "error" in compiled_or_error:
        return compiled_or_error
    compiled = compiled_or_error

    try:
        result = await get_user_sql_executor().execute(
            user_id=user_id,
            sql=compiled["sql"],
            params=compiled["params"],
            is_admin=is_admin,
            vault_names=[vault_name],
        )
    except PermissionDeniedError as e:
        return err(str(e), code=PERMISSION_DENIED, pg_sqlstate=e.pg_sqlstate)
    except asyncpg.PostgresError as e:
        return err(str(e), code=SQL_ERROR, pg_sqlstate=getattr(e, "sqlstate", None))

    body, content_range = _shape_result(
        result,
        vault_name=vault_name,
        table_name=table_name,
        projections=compiled["projections"],
        count_exact=compiled["count_exact"],
        offset=compiled["page"].offset,
    )
    return RowQueryResponse(body=body, content_range=content_range)


def compile_row_query(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    query_params: Sequence[tuple[str, str]],
    range_header: str | None = None,
    prefer_header: str | None = None,
) -> dict[str, Any]:
    column_meta = _column_meta(columns)
    params: list[Any] = []

    page_or_error = _parse_page(query_params, range_header)
    if isinstance(page_or_error, dict):
        return page_or_error
    page = page_or_error

    projections_or_error = _compile_select(_last_value(query_params, "select"), column_meta, params)
    if isinstance(projections_or_error, dict):
        return projections_or_error
    projections = projections_or_error

    where_or_error = _compile_filters(query_params, column_meta, params)
    if isinstance(where_or_error, dict):
        return where_or_error
    order_or_error = _compile_order(_last_value(query_params, "order"), column_meta, params)
    if isinstance(order_or_error, dict):
        return order_or_error

    select_sql = ", ".join(p.sql for p in projections)
    count_exact = _prefer_count_exact(prefer_header)
    from_sql = f"FROM {table_data_repo.pg_table_name(vault_name, table_name)}"
    if where_or_error:
        from_sql += f" WHERE {where_or_error}"

    if count_exact:
        page_sql = f"SELECT {select_sql}, TRUE AS __akb_present {from_sql}"
        if order_or_error:
            page_sql += f" ORDER BY {order_or_error}"
        page_sql += f" LIMIT {page.limit} OFFSET {page.offset}"
        sql = (
            f"WITH __akb_count AS (SELECT count(*) AS __akb_total {from_sql}), "
            f"__akb_page AS ({page_sql}) "
            "SELECT __akb_page.*, __akb_count.__akb_total "
            "FROM __akb_count LEFT JOIN __akb_page ON TRUE"
        )
    else:
        sql = f"SELECT {select_sql} {from_sql}"
        if order_or_error:
            sql += f" ORDER BY {order_or_error}"
        sql += f" LIMIT {page.limit} OFFSET {page.offset}"

    return {
        "sql": sql,
        "params": params,
        "projections": projections,
        "count_exact": count_exact,
        "page": page,
    }


def _column_meta(columns: list[dict]) -> dict[str, str]:
    meta = dict(BOOKKEEPING_COLUMNS)
    for col in columns:
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        if isinstance(name, str) and name:
            meta[name] = str(col.get("type") or "text").lower()
    return meta


def _compile_select(
    select_value: str | None,
    column_meta: dict[str, str],
    params: list[Any],
) -> list[_Projection] | dict[str, Any]:
    if not select_value:
        return [_Projection(sql="*", output_key="*", result_key="*")]
    projections: list[_Projection] = []
    for idx, token in enumerate(_split_top_level(select_value)):
        token = token.strip()
        if not token:
            continue
        if re.search(r"(?<!:):(?!:)", token):
            return err("Column aliases in select= are not implemented yet.", code="not_implemented")
        if token == "*":
            projections.append(_Projection(sql="*", output_key="*", result_key="*"))
            continue
        operand_or_error = _compile_operand(token, column_meta)
        if isinstance(operand_or_error, dict):
            return operand_or_error
        operand = _bind_operand_params(operand_or_error, params)
        if operand.sql == token:
            projections.append(_Projection(sql=operand.sql, output_key=token, result_key=token))
        else:
            result_key = f"__akb_col_{idx}"
            projections.append(_Projection(sql=f"{operand.sql} AS {result_key}", output_key=token, result_key=result_key))
    return projections or [_Projection(sql="*", output_key="*", result_key="*")]


def _compile_filters(
    query_params: Sequence[tuple[str, str]],
    column_meta: dict[str, str],
    params: list[Any],
) -> str | dict[str, Any]:
    clauses: list[str] = []
    for key, value in query_params:
        if key in {"select", "order", "limit", "offset"}:
            continue
        if key in {"or", "and"}:
            clause_or_error = _compile_bool_group(key, value, column_meta, params, depth=1)
        else:
            clause_or_error = _compile_condition(key, value, column_meta, params)
        if isinstance(clause_or_error, dict):
            return clause_or_error
        if clause_or_error:
            clauses.append(clause_or_error)
    return " AND ".join(f"({c})" for c in clauses)


def _compile_bool_group(
    joiner: str,
    value: str,
    column_meta: dict[str, str],
    params: list[Any],
    *,
    depth: int,
) -> str | dict[str, Any]:
    if depth > MAX_BOOL_DEPTH:
        return err("Boolean filter nesting is too deep.", code="filter_too_deep")
    inner = value.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    clauses: list[str] = []
    for part in _split_top_level(inner):
        part = part.strip()
        if not part:
            continue
        nested = _nested_group(part)
        if nested:
            nested_joiner, nested_value = nested
            clause_or_error = _compile_bool_group(
                nested_joiner, nested_value, column_meta, params, depth=depth + 1,
            )
        else:
            field, op_value = _split_bool_condition(part)
            if field is None or op_value is None:
                return err(f"Invalid boolean filter: {part}", code="invalid_filter")
            clause_or_error = _compile_condition(field, op_value, column_meta, params)
        if isinstance(clause_or_error, dict):
            return clause_or_error
        clauses.append(clause_or_error)
    if not clauses:
        return err(f"Invalid boolean filter: empty {joiner} group.", code="invalid_filter")
    glue = " OR " if joiner == "or" else " AND "
    return glue.join(f"({c})" for c in clauses)


def _compile_condition(
    field: str,
    raw_value: str,
    column_meta: dict[str, str],
    params: list[Any],
) -> str | dict[str, Any]:
    operand_or_error = _compile_operand(field, column_meta)
    if isinstance(operand_or_error, dict):
        return operand_or_error
    operand = _bind_operand_params(operand_or_error, params)
    operator, value = _split_operator(raw_value)
    if operator is None:
        return err(f"Invalid filter value for {field}: expected op.value", code="invalid_filter")
    return _compile_operator(operand, operator, value, params)


def _compile_operator(
    operand: _Operand,
    operator: str,
    value: str,
    params: list[Any],
) -> str | dict[str, Any]:
    if operator == "not":
        inner_op, inner_value = _split_operator(value)
        if inner_op is None:
            return err("Invalid not filter: expected not.op.value", code="invalid_filter")
        inner = _compile_operator(operand, inner_op, inner_value, params)
        if isinstance(inner, dict):
            return inner
        return f"NOT ({inner})"
    if operator == "is":
        lowered = value.lower()
        if lowered == "null":
            return f"{operand.sql} IS NULL"
        if lowered in {"true", "false"}:
            return f"{operand.sql} IS {lowered.upper()}"
        return err("is operator only supports null, true, or false.", code="invalid_filter")
    if operator in {"eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike"}:
        sql_op = {
            "eq": "=",
            "neq": "<>",
            "gt": ">",
            "gte": ">=",
            "lt": "<",
            "lte": "<=",
            "like": "LIKE",
            "ilike": "ILIKE",
        }[operator]
        type_name = "text" if operator in {"like", "ilike"} else operand.type_name
        converted = _convert_value(value.replace("*", "%") if operator in {"like", "ilike"} else value, type_name)
        if isinstance(converted, dict):
            return converted
        return f"{operand.sql} {sql_op} {_add_param(params, converted)}"
    if operator == "in":
        values = _parse_in_values(value, operand.type_name)
        if isinstance(values, dict):
            return values
        return f"{operand.sql} = ANY({_add_param(params, values)})"
    if operator == "cs":
        contains_value = _parse_contains_value(value, operand.type_name)
        if isinstance(contains_value, dict):
            return contains_value
        cast = "::jsonb" if operand.type_name == "json" else ""
        return f"{operand.sql} @> {_add_param(params, contains_value)}{cast}"
    return err(f"Unknown row-read operator: {operator}", code="invalid_operator")


def _compile_order(
    order_value: str | None,
    column_meta: dict[str, str],
    params: list[Any],
) -> str | dict[str, Any]:
    if not order_value:
        return ""
    parts: list[str] = []
    for token in _split_top_level(order_value):
        bits = token.rsplit(".", 1)
        direction = "ASC"
        field = token
        if len(bits) == 2 and bits[1].lower() in {"asc", "desc"}:
            field, direction = bits[0], bits[1].upper()
        operand_or_error = _compile_operand(field, column_meta)
        if isinstance(operand_or_error, dict):
            return operand_or_error
        operand = _bind_operand_params(operand_or_error, params)
        parts.append(f"{operand.sql} {direction}")
    return ", ".join(parts)


def _compile_operand(raw: str, column_meta: dict[str, str]) -> _Operand | dict[str, Any]:
    token = raw.strip()
    m = _JSON_PATH_RE.fullmatch(token)
    if not m:
        return _unknown_column(token, column_meta)
    base = m.group("base")
    if base not in column_meta:
        return _unknown_column(base, column_meta)
    arrow = m.group("arrow")
    cast = m.group("cast")
    if cast and cast not in CAST_SQL:
        return err(f"Invalid JSON cast {cast!r}.", code="invalid_cast", allowed_casts=sorted(CAST_SQL))
    if not arrow:
        if cast:
            return err("Casts are only supported for JSON path operands.", code="invalid_cast")
        ident = table_data_repo.safe_ident(base)
        return _Operand(sql=ident, params=[], type_name=column_meta[base])
    if column_meta[base] != "json":
        return err(f"Column {base!r} is not a JSON column.", code=UNDEFINED_COLUMN)
    path = (m.group("path") or "").strip()
    if not path:
        return err(f"Invalid JSON path operand: {raw}", code="invalid_filter")
    sql_base = table_data_repo.safe_ident(base)
    if arrow == "->>":
        expr = f"{sql_base} ->> ${{param}}::text"
        operand_params: list[Any] = [path]
    else:
        path_items = _parse_json_path_list(path)
        if isinstance(path_items, dict):
            return path_items
        expr = f"{sql_base} #>> ${{param}}::text[]"
        operand_params = [path_items]
    type_name = cast or "text"
    if cast:
        expr = f"({expr})::{CAST_SQL[cast]}"
    return _Operand(sql=expr, params=operand_params, type_name=type_name)


def _bind_operand_params(operand: _Operand, params: list[Any]) -> _Operand:
    sql = operand.sql
    for value in operand.params:
        sql = sql.replace("${param}", _add_param(params, value), 1)
    return _Operand(sql=sql, params=[], type_name=operand.type_name)


def _add_param(params: list[Any], value: Any) -> str:
    params.append(value)
    return f"${len(params)}"


def _parse_page(query_params: Sequence[tuple[str, str]], range_header: str | None) -> _Page | dict[str, Any]:
    limit = _parse_int(_last_value(query_params, "limit"), default=DEFAULT_LIMIT)
    offset = _parse_int(_last_value(query_params, "offset"), default=0)
    if isinstance(limit, dict):
        return limit
    if isinstance(offset, dict):
        return offset
    if range_header:
        m = re.fullmatch(r"\s*(\d+)-(\d+)\s*", range_header)
        if not m:
            return err("Invalid Range header; expected N-M.", code="invalid_argument")
        start, end = int(m.group(1)), int(m.group(2))
        if end < start:
            return err("Invalid Range header; end must be >= start.", code="invalid_argument")
        offset = start
        limit = end - start + 1
    if limit < 0 or offset < 0:
        return err("limit and offset must be non-negative.", code="invalid_argument")
    return _Page(limit=min(limit, MAX_LIMIT), offset=offset)


def _parse_int(value: str | None, *, default: int) -> int | dict[str, Any]:
    if value in {None, ""}:
        return default
    try:
        return int(str(value))
    except ValueError:
        return err(f"Expected integer, got {value!r}.", code="invalid_argument")


def _prefer_count_exact(prefer_header: str | None) -> bool:
    return bool(prefer_header and "count=exact" in prefer_header.lower())


def _last_value(query_params: Sequence[tuple[str, str]], key: str) -> str | None:
    values = [v for k, v in query_params if k == key]
    return values[-1] if values else None


def _split_operator(raw_value: str) -> tuple[str | None, str]:
    if "." not in raw_value:
        return None, raw_value
    op, value = raw_value.split(".", 1)
    return op.lower(), value


def _split_bool_condition(raw: str) -> tuple[str | None, str | None]:
    first = raw.find(".")
    if first == -1:
        return None, None
    return raw[:first], raw[first + 1:]


def _nested_group(raw: str) -> tuple[str, str] | None:
    for name in ("or", "and"):
        prefix = f"{name}("
        if raw.startswith(prefix) and raw.endswith(")"):
            return name, raw[len(name):]
    return None


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    brace_depth = 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
        if ch == "," and depth == 0 and brace_depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_json_path_list(raw: str) -> list[str] | dict[str, Any]:
    text = raw.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return err("#>> JSON path must use {a,b} syntax.", code="invalid_filter")
    return [p.strip() for p in text[1:-1].split(",") if p.strip()]


def _parse_in_values(raw: str, type_name: str) -> list[Any] | dict[str, Any]:
    text = raw.strip()
    if not (text.startswith("(") and text.endswith(")")):
        return err("in operator expects parenthesized values, e.g. in.(a,b).", code="invalid_filter")
    out: list[Any] = []
    for item in _split_top_level(text[1:-1]):
        converted = _convert_value(item.strip(), type_name)
        if isinstance(converted, dict):
            return converted
        out.append(converted)
    return out


def _parse_contains_value(raw: str, type_name: str) -> Any | dict[str, Any]:
    if type_name != "json":
        return err("cs operator is only supported for JSON columns in this table API.", code="invalid_operator")
    text = raw.strip()
    if text.startswith("{") and text.endswith("}") and ":" not in text:
        return json.dumps([p.strip() for p in text[1:-1].split(",") if p.strip()])
    try:
        return json.dumps(json.loads(text))
    except ValueError:
        return err("cs on JSON columns expects JSON or {a,b} syntax.", code="invalid_filter")


def _convert_value(raw: str, type_name: str) -> Any | dict[str, Any]:
    try:
        if type_name in {"text", "json"}:
            return raw
        if type_name in {"number", "numeric"}:
            return Decimal(raw)
        if type_name == "int":
            return int(raw)
        if type_name == "float":
            return float(raw)
        if type_name in {"boolean", "bool"}:
            lowered = raw.lower()
            if lowered in {"true", "t", "1"}:
                return True
            if lowered in {"false", "f", "0"}:
                return False
            raise ValueError
        if type_name == "date":
            return date.fromisoformat(raw)
        if type_name == "timestamp":
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if type_name == "uuid":
            return uuid.UUID(raw)
    except (ValueError, InvalidOperation):
        return err(f"Could not convert {raw!r} to {type_name}.", code="invalid_argument")
    return raw


def _unknown_column(name: str, column_meta: dict[str, str]) -> dict[str, Any]:
    available = sorted(column_meta)
    return err(
        f"Column {name!r} does not exist on this table.",
        code=UNDEFINED_COLUMN,
        hint=fuzzy_hint(name, available, label="columns"),
        available_columns=available,
    )


def _shape_result(
    result: dict[str, Any],
    *,
    vault_name: str,
    table_name: str,
    projections: Sequence[_Projection],
    count_exact: bool,
    offset: int,
) -> tuple[dict[str, Any], str | None]:
    rows = result.get("items", [])
    total = len(rows)
    if count_exact and rows:
        total = int(rows[0].get("__akb_total") or 0)
    visible_rows = [
        row for row in rows
        if not count_exact or row.get("__akb_present")
    ]
    shaped_items = [_shape_item(row, projections) for row in visible_rows]
    columns = _output_columns(result.get("columns", []), projections)
    body = {
        "kind": "table_query",
        "vault": vault_name,
        "table": table_name,
        "columns": columns,
        "items": shaped_items,
        "total": total if count_exact else len(shaped_items),
    }
    if not count_exact:
        return body, None
    if shaped_items:
        return body, f"{offset}-{offset + len(shaped_items) - 1}/{total}"
    return body, f"*/{total}"


def _shape_item(row: dict[str, Any], projections: Sequence[_Projection]) -> dict[str, Any]:
    if any(p.output_key == "*" for p in projections):
        out = {
            k: v for k, v in row.items()
            if k not in {"__akb_total", "__akb_present"} and not k.startswith("__akb_col_")
        }
    else:
        out = {}
    for p in projections:
        if p.output_key != "*":
            out[p.output_key] = row.get(p.result_key)
    return out


def _output_columns(raw_columns: Iterable[str], projections: Sequence[_Projection]) -> list[str]:
    if any(p.output_key == "*" for p in projections):
        columns = [
            c for c in raw_columns
            if c not in {"__akb_total", "__akb_present"} and not c.startswith("__akb_col_")
        ]
    else:
        columns = []
    for p in projections:
        if p.output_key != "*":
            columns.append(p.output_key)
    return columns
