"""Result shaping for vault-table row reads.

Turns the raw executor rows into the `table_query` response body,
stripping the bookkeeping columns injected for exact-count paging and
restoring caller-facing projection keys.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from app.services.row_query_base import _Projection


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
