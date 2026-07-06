"""PostgREST-style querystring pipeline for vault-table row reads.

Compiles URL query parameters (`col=op.value`, `or=(...)`, `select=`,
`order=`) into one parameterized SELECT. Shared operand/value/select/
order primitives live in `row_query_base`.
"""

from __future__ import annotations

from typing import Any, Sequence

from app.util.errors import (
    FILTER_TOO_DEEP,
    INVALID_FILTER,
    INVALID_OPERATOR,
    err,
)
from app.services.row_query_base import (
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
    _last_value,
    _nested_group,
    _parse_contains_value,
    _parse_in_values,
    _parse_page,
    _prefer_count_exact,
    _split_bool_condition,
    _split_operator,
    _split_top_level,
)


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

    return _finish_row_query(
        vault_name=vault_name,
        table_name=table_name,
        projections=projections,
        where_sql=where_or_error,
        order_sql=order_or_error,
        page=page,
        params=params,
        count_exact=_prefer_count_exact(prefer_header),
    )


def _compile_filters(
    query_params: Sequence[tuple[str, str]],
    column_meta: dict[str, str],
    params: list[Any],
) -> str | dict[str, Any]:
    clauses: list[str] = []
    for key, value in query_params:
        # A real column can share a name with a reserved query-control param
        # (e.g. a table with a "select"/"order"/"limit"/"offset" column) —
        # column identity wins so the filter is never silently dropped from
        # the WHERE clause. Shared by both row-read and row-write callers.
        if key in {"select", "order", "limit", "offset"} and key not in column_meta:
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
        return err("Boolean filter nesting is too deep.", code=FILTER_TOO_DEEP)
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
                return err(f"Invalid boolean filter: {part}", code=INVALID_FILTER)
            clause_or_error = _compile_condition(field, op_value, column_meta, params)
        if isinstance(clause_or_error, dict):
            return clause_or_error
        clauses.append(clause_or_error)
    if not clauses:
        return err(f"Invalid boolean filter: empty {joiner} group.", code=INVALID_FILTER)
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
        return err(f"Invalid filter value for {field}: expected op.value", code=INVALID_FILTER)
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
            return err("Invalid not filter: expected not.op.value", code=INVALID_FILTER)
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
        cast = "::jsonb" if _is_json_type(operand.type_name) else ""
        return f"{operand.sql} @> {_add_param(params, contains_value)}{cast}"
    return err(f"Unknown row-read operator: {operator}", code=INVALID_OPERATOR)
