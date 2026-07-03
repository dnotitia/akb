"""JSON-AST pipeline for vault-table row reads.

Compiles a structured read AST (`{"select": ..., "filter": {"and":
[...]}, "order": [...]}`) into one parameterized SELECT. Shares operand/
value/select/order primitives with the querystring pipeline via
`row_query_base`.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from app.repositories import table_data_repo
from app.util.errors import (
    FILTER_TOO_DEEP,
    INVALID_ARGUMENT,
    INVALID_CAST,
    INVALID_FILTER,
    INVALID_OPERATOR,
    METHOD_NOT_ALLOWED,
    UNDEFINED_COLUMN,
    err,
)
from app.services.row_query_base import (
    CAST_SQL,
    MAX_BOOL_DEPTH,
    _Operand,
    _add_param,
    _bind_operand_params,
    _column_meta,
    _compile_operand,
    _compile_order,
    _compile_select,
    _convert_value,
    _finish_row_query,
    _is_json_type,
    _parse_contains_value,
    _parse_page,
    _prefer_count_exact,
    _unknown_column,
)


def compile_ast_row_query(
    *,
    vault_name: str,
    table_name: str,
    columns: list[dict],
    ast: Mapping[str, Any],
    range_header: str | None = None,
    prefer_header: str | None = None,
) -> dict[str, Any]:
    forbidden_writes = sorted({"insert", "update", "delete", "upsert"} & set(ast))
    if forbidden_writes:
        return err(
            "POST /query supports read ASTs only.",
            code=METHOD_NOT_ALLOWED,
            unsupported_keys=forbidden_writes,
        )

    column_meta = _column_meta(columns)
    params: list[Any] = []

    page_or_error = _parse_page(_ast_page_params(ast), range_header)
    if isinstance(page_or_error, dict):
        return page_or_error
    page = page_or_error

    select_or_error = _ast_select_value(ast.get("select"))
    if isinstance(select_or_error, dict):
        return select_or_error
    projections_or_error = _compile_select(select_or_error, column_meta, params)
    if isinstance(projections_or_error, dict):
        return projections_or_error
    projections = projections_or_error

    filter_node = _ast_filter_root(ast)
    where_sql = ""
    if filter_node is not None:
        compiled_filter = _compile_ast_filter(filter_node, column_meta, params, depth=1)
        if isinstance(compiled_filter, dict):
            return compiled_filter
        where_sql = compiled_filter

    order_or_error = _compile_ast_order(ast.get("order"), column_meta, params)
    if isinstance(order_or_error, dict):
        return order_or_error

    return _finish_row_query(
        vault_name=vault_name,
        table_name=table_name,
        projections=projections,
        where_sql=where_sql,
        order_sql=order_or_error,
        page=page,
        params=params,
        count_exact=_prefer_count_exact(prefer_header) or _ast_count_exact(ast.get("count")),
    )


def _ast_page_params(ast: Mapping[str, Any]) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for key in ("limit", "offset"):
        if key in ast and ast[key] is not None:
            params.append((key, str(ast[key])))
    page = ast.get("page")
    if isinstance(page, Mapping):
        for key in ("limit", "offset"):
            if key in page and page[key] is not None:
                params.append((key, str(page[key])))
    return params


def _ast_select_value(value: Any) -> str | None | dict[str, Any]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return err("AST select entries must be strings.", code=INVALID_ARGUMENT)
            out.append(item)
        return ",".join(out)
    return err("AST select must be a string or string array.", code=INVALID_ARGUMENT)


def _ast_filter_root(ast: Mapping[str, Any]) -> Any | None:
    for key in ("filter", "where"):
        if key in ast:
            return ast[key]
    if any(key in ast for key in ("and", "or", "col", "jsonb")):
        return ast
    return None


def _compile_ast_filter(
    node: Any,
    column_meta: dict[str, str],
    params: list[Any],
    *,
    depth: int,
) -> str | dict[str, Any]:
    if not isinstance(node, Mapping):
        return err("AST filter nodes must be objects.", code=INVALID_ARGUMENT)
    group_keys = [key for key in ("and", "or") if key in node]
    if group_keys:
        if len(group_keys) != 1:
            return err("AST filter node must contain only one boolean group.", code=INVALID_ARGUMENT)
        return _compile_ast_bool_group(group_keys[0], node[group_keys[0]], column_meta, params, depth=depth)
    return _compile_ast_condition(node, column_meta, params)


def _compile_ast_bool_group(
    joiner: str,
    value: Any,
    column_meta: dict[str, str],
    params: list[Any],
    *,
    depth: int,
) -> str | dict[str, Any]:
    if depth > MAX_BOOL_DEPTH:
        return err("Boolean filter nesting is too deep.", code=FILTER_TOO_DEEP)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return err(f"AST {joiner} filter must be an array.", code=INVALID_ARGUMENT)
    clauses: list[str] = []
    for item in value:
        clause_or_error = _compile_ast_filter(item, column_meta, params, depth=depth + 1)
        if isinstance(clause_or_error, dict):
            return clause_or_error
        if clause_or_error:
            clauses.append(clause_or_error)
    if not clauses:
        return err(f"Invalid boolean filter: empty {joiner} group.", code=INVALID_FILTER)
    glue = " OR " if joiner == "or" else " AND "
    return f"({glue.join(f'({c})' for c in clauses)})"


def _compile_ast_condition(
    node: Mapping[str, Any],
    column_meta: dict[str, str],
    params: list[Any],
) -> str | dict[str, Any]:
    operator = node.get("op")
    if not isinstance(operator, str) or not operator:
        return err("AST condition must include an operator string.", code=INVALID_ARGUMENT)
    operand_or_error = _compile_ast_operand(node, column_meta)
    if isinstance(operand_or_error, dict):
        return operand_or_error
    operand = _bind_operand_params(operand_or_error, params)
    return _compile_ast_operator(operand, operator.lower(), node.get("val"), params)


def _compile_ast_operand(node: Mapping[str, Any], column_meta: dict[str, str]) -> _Operand | dict[str, Any]:
    col = node.get("col")
    if isinstance(col, str):
        return _compile_operand(col, column_meta)
    jsonb = node.get("jsonb")
    if not isinstance(jsonb, Mapping):
        return err("AST condition must include col or jsonb.", code=INVALID_ARGUMENT)
    base = jsonb.get("col")
    if not isinstance(base, str) or not base:
        return err("AST jsonb operand must include a column name.", code=INVALID_ARGUMENT)
    if base not in column_meta:
        return _unknown_column(base, column_meta)
    if not _is_json_type(column_meta[base]):
        return err(f"Column {base!r} is not a JSON column.", code=UNDEFINED_COLUMN)
    path = jsonb.get("path")
    if not isinstance(path, Sequence) or isinstance(path, (str, bytes, bytearray)) or not path:
        return err("AST jsonb operand must include a non-empty path array.", code=INVALID_ARGUMENT)
    path_items: list[str] = []
    for item in path:
        if not isinstance(item, str) or not item:
            return err("AST jsonb path entries must be non-empty strings.", code=INVALID_ARGUMENT)
        path_items.append(item)
    cast = jsonb.get("cast")
    if cast is not None and (not isinstance(cast, str) or cast not in CAST_SQL):
        return err(f"Invalid JSON cast {cast!r}.", code=INVALID_CAST, allowed_casts=sorted(CAST_SQL))
    sql_base = table_data_repo.safe_ident(base)
    if len(path_items) == 1:
        expr = f"{sql_base} ->> ${{param}}::text"
        operand_params: list[Any] = [path_items[0]]
    else:
        expr = f"{sql_base} #>> ${{param}}::text[]"
        operand_params = [path_items]
    type_name = cast or "text"
    if cast:
        expr = f"({expr})::{CAST_SQL[cast]}"
    return _Operand(sql=expr, params=operand_params, type_name=type_name)


def _compile_ast_operator(
    operand: _Operand,
    operator: str,
    value: Any,
    params: list[Any],
) -> str | dict[str, Any]:
    if operator.startswith("not."):
        inner = _compile_ast_operator(operand, operator.removeprefix("not."), value, params)
        if isinstance(inner, dict):
            return inner
        return f"NOT ({inner})"
    if operator == "not":
        if not isinstance(value, Mapping):
            return err("AST not operator expects an object value.", code=INVALID_ARGUMENT)
        inner_op = value.get("op")
        if not isinstance(inner_op, str) or not inner_op:
            return err("AST not operator expects an inner operator.", code=INVALID_ARGUMENT)
        inner = _compile_ast_operator(operand, inner_op.lower(), value.get("val"), params)
        if isinstance(inner, dict):
            return inner
        return f"NOT ({inner})"
    if operator == "is":
        lowered = "null" if value is None else str(value).lower()
        if lowered == "null":
            return f"{operand.sql} IS NULL"
        if lowered in {"true", "false"}:
            return f"{operand.sql} IS {lowered.upper()}"
        return err("is operator only supports null, true, or false.", code=INVALID_FILTER)
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
        raw_value = str(value).replace("*", "%") if operator in {"like", "ilike"} else value
        converted = _convert_ast_value(raw_value, type_name)
        if isinstance(converted, dict):
            return converted
        return f"{operand.sql} {sql_op} {_add_param(params, converted)}"
    if operator == "in":
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return err("AST in operator expects an array value.", code=INVALID_ARGUMENT)
        values: list[Any] = []
        for item in value:
            converted = _convert_ast_value(item, operand.type_name)
            if isinstance(converted, dict):
                return converted
            values.append(converted)
        return f"{operand.sql} = ANY({_add_param(params, values)})"
    if operator == "cs":
        if not _is_json_type(operand.type_name):
            return err("cs operator is only supported for JSON columns in this table API.", code=INVALID_OPERATOR)
        contains_value = json.dumps(value) if isinstance(value, (Mapping, list)) else _parse_contains_value(str(value), "json")
        if isinstance(contains_value, dict):
            return contains_value
        return f"{operand.sql} @> {_add_param(params, contains_value)}::jsonb"
    return err(f"Unknown row-read operator: {operator}", code=INVALID_OPERATOR)


def _compile_ast_order(
    value: Any,
    column_meta: dict[str, str],
    params: list[Any],
) -> str | dict[str, Any]:
    if value is None:
        return ""
    values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, Mapping)) else [value]
    parts: list[str] = []
    for item in values:
        if isinstance(item, str):
            order_or_error = _compile_order(item, column_meta, params)
            if isinstance(order_or_error, dict):
                return order_or_error
            if order_or_error:
                parts.append(order_or_error)
            continue
        if not isinstance(item, Mapping):
            return err("AST order entries must be strings or objects.", code=INVALID_ARGUMENT)
        direction = str(item.get("dir") or item.get("direction") or "asc").lower()
        if direction not in {"asc", "desc"}:
            return err("AST order direction must be asc or desc.", code=INVALID_ARGUMENT)
        operand_or_error = _compile_ast_operand(item, column_meta)
        if isinstance(operand_or_error, dict):
            return operand_or_error
        operand = _bind_operand_params(operand_or_error, params)
        parts.append(f"{operand.sql} {direction.upper()}")
    return ", ".join(parts)


def _ast_count_exact(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.lower() == "exact"


def _convert_ast_value(value: Any, type_name: str) -> Any | dict[str, Any]:
    if value is None:
        return err("Filter value cannot be null; use the is operator.", code=INVALID_ARGUMENT)
    if _is_json_type(type_name) and not isinstance(value, str):
        return json.dumps(value)
    return _convert_value(str(value), type_name)
