"""Shared primitives for the vault-table row-read compilers.

Constants, dataclasses, operand parsing, value conversion, and the
final SELECT assembly used by both the querystring pipeline
(`row_query_string`) and the JSON-AST pipeline (`row_query_ast`).
This module has no dependency on either pipeline.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from app.repositories import table_data_repo
from app.util.errors import (
    INVALID_ARGUMENT,
    INVALID_CAST,
    INVALID_FILTER,
    INVALID_OPERATOR,
    NOT_IMPLEMENTED,
    UNDEFINED_COLUMN,
    err,
)
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


def _is_json_type(type_name: str) -> bool:
    return type_name in {"json", "jsonb"}
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


def _column_meta(columns: list[dict]) -> dict[str, str]:
    meta = dict(BOOKKEEPING_COLUMNS)
    for col in columns:
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        if isinstance(name, str) and name:
            meta[name] = str(col.get("type") or "text").lower()
    return meta


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
        return err(f"Invalid JSON cast {cast!r}.", code=INVALID_CAST, allowed_casts=sorted(CAST_SQL))
    if not arrow:
        if cast:
            return err("Casts are only supported for JSON path operands.", code=INVALID_CAST)
        ident = table_data_repo.safe_ident(base)
        return _Operand(sql=ident, params=[], type_name=column_meta[base])
    if not _is_json_type(column_meta[base]):
        return err(f"Column {base!r} is not a JSON column.", code=UNDEFINED_COLUMN)
    path = (m.group("path") or "").strip()
    if not path:
        return err(f"Invalid JSON path operand: {raw}", code=INVALID_FILTER)
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
            return err("Invalid Range header; expected N-M.", code=INVALID_ARGUMENT)
        start, end = int(m.group(1)), int(m.group(2))
        if end < start:
            return err("Invalid Range header; end must be >= start.", code=INVALID_ARGUMENT)
        offset = start
        limit = end - start + 1
    if limit < 0 or offset < 0:
        return err("limit and offset must be non-negative.", code=INVALID_ARGUMENT)
    return _Page(limit=min(limit, MAX_LIMIT), offset=offset)


def _parse_int(value: str | None, *, default: int) -> int | dict[str, Any]:
    if value in {None, ""}:
        return default
    try:
        return int(str(value))
    except ValueError:
        return err(f"Expected integer, got {value!r}.", code=INVALID_ARGUMENT)


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
        return err("#>> JSON path must use {a,b} syntax.", code=INVALID_FILTER)
    return [p.strip() for p in text[1:-1].split(",") if p.strip()]


def _parse_in_values(raw: str, type_name: str) -> list[Any] | dict[str, Any]:
    text = raw.strip()
    if not (text.startswith("(") and text.endswith(")")):
        return err("in operator expects parenthesized values, e.g. in.(a,b).", code=INVALID_FILTER)
    out: list[Any] = []
    for item in _split_top_level(text[1:-1]):
        converted = _convert_value(item.strip(), type_name)
        if isinstance(converted, dict):
            return converted
        out.append(converted)
    return out


def _parse_contains_value(raw: str, type_name: str) -> Any | dict[str, Any]:
    if not _is_json_type(type_name):
        return err("cs operator is only supported for JSON columns in this table API.", code=INVALID_OPERATOR)
    text = raw.strip()
    if text.startswith("{") and text.endswith("}") and ":" not in text:
        return json.dumps([p.strip() for p in text[1:-1].split(",") if p.strip()])
    try:
        return json.dumps(json.loads(text))
    except ValueError:
        return err("cs on JSON columns expects JSON or {a,b} syntax.", code=INVALID_FILTER)


def _convert_value(raw: str, type_name: str) -> Any | dict[str, Any]:
    try:
        if type_name in {"text", "json", "jsonb"}:
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
        return err(f"Could not convert {raw!r} to {type_name}.", code=INVALID_ARGUMENT)
    return raw


def _unknown_column(name: str, column_meta: dict[str, str]) -> dict[str, Any]:
    available = sorted(column_meta)
    return err(
        f"Column {name!r} does not exist on this table.",
        code=UNDEFINED_COLUMN,
        hint=fuzzy_hint(name, available, label="columns"),
        available_columns=available,
    )


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
            return err("Column aliases in select= are not implemented yet.", code=NOT_IMPLEMENTED)
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


def _finish_row_query(
    *,
    vault_name: str,
    table_name: str,
    projections: list[_Projection],
    where_sql: str,
    order_sql: str,
    page: _Page,
    params: list[Any],
    count_exact: bool,
) -> dict[str, Any]:
    select_sql = ", ".join(p.sql for p in projections)
    from_sql = f"FROM {table_data_repo.pg_table_name(vault_name, table_name)}"
    if where_sql:
        from_sql += f" WHERE {where_sql}"

    if count_exact:
        page_sql = f"SELECT {select_sql}, TRUE AS __akb_present {from_sql}"
        if order_sql:
            page_sql += f" ORDER BY {order_sql}"
        page_sql += f" LIMIT {page.limit} OFFSET {page.offset}"
        sql = (
            f"WITH __akb_count AS (SELECT count(*) AS __akb_total {from_sql}), "
            f"__akb_page AS ({page_sql}) "
            "SELECT __akb_page.*, __akb_count.__akb_total "
            "FROM __akb_count LEFT JOIN __akb_page ON TRUE"
        )
    else:
        sql = f"SELECT {select_sql} {from_sql}"
        if order_sql:
            sql += f" ORDER BY {order_sql}"
        sql += f" LIMIT {page.limit} OFFSET {page.offset}"

    return {
        "sql": sql,
        "params": params,
        "projections": projections,
        "count_exact": count_exact,
        "page": page,
    }
