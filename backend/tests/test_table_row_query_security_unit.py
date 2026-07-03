"""Security invariants for the row-read compiler.

The compiler may emit table and column identifiers, but caller-controlled
values must only appear as asyncpg parameters. These tests lock that down for
both URL query and JSON-AST frontends.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.repositories.table_data_repo import count_statement_separators
from app.services.table_row_query import compile_ast_row_query, compile_row_query


COLUMNS = [
    {"name": "title", "type": "text"},
    {"name": "severity", "type": "text"},
    {"name": "score", "type": "number"},
    {"name": "metadata", "type": "json"},
]

SCALAR_OPS = ["eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike"]
ADVERSARIAL_VALUES = [
    "x'; DROP TABLE users;--",
    "$$x; SELECT pg_read_file('/etc/passwd'); $$",
    "x/*comment*/; SELECT 1",
    "한글'; --",
    "metadata->>tier",
    "$1 OR TRUE",
    "a,b)c",
]
BAD_IDENTIFIERS = [
    [("title;DROP TABLE users;--", "eq.safe")],
    [("sevverity", "eq.high")],
    [("metadata->>tier::money", "eq.gold")],
    [("order", "title;DROP.desc")],
    [("or", "(title.eq.safe,unknown.eq.leak)")],
]


def _flatten(values: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if isinstance(value, list):
            out.extend(_flatten(value))
        else:
            out.append(value)
    return out


def _assert_single_statement_and_values_bound(compiled: dict[str, Any]) -> None:
    assert "error" not in compiled
    assert count_statement_separators(compiled["sql"]) == 0
    for value in _flatten(compiled["params"]):
        if isinstance(value, str) and value:
            assert value not in compiled["sql"]


@settings(max_examples=60, deadline=None)
@given(
    op=st.sampled_from(SCALAR_OPS),
    value=st.text(min_size=1, max_size=30).map(lambda s: f"__akb_payload_{s}__"),
)
def test_url_scalar_values_are_always_bound(op: str, value: str) -> None:
    compiled = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("title", f"{op}.{value}")],
    )

    _assert_single_statement_and_values_bound(compiled)
    expected = value.replace("*", "%") if op in {"like", "ilike"} else value
    assert compiled["params"] == [expected]


@settings(max_examples=60, deadline=None)
@given(
    values=st.lists(
        st.text(min_size=1, max_size=24).map(lambda s: f"__akb_in_{s}__"),
        min_size=1,
        max_size=4,
    ),
)
def test_ast_in_values_are_always_bound(values: list[str]) -> None:
    compiled = compile_ast_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        ast={"filter": {"col": "title", "op": "in", "val": values}},
    )

    _assert_single_statement_and_values_bound(compiled)
    assert compiled["params"] == [values]


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_curated_adversarial_values_are_bound_for_both_frontends(value: str) -> None:
    url = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("title", f"eq.{value}")],
    )
    ast = compile_ast_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        ast={"filter": {"col": "title", "op": "eq", "val": value}},
    )

    _assert_single_statement_and_values_bound(url)
    _assert_single_statement_and_values_bound(ast)
    assert url["params"] == [value]
    assert ast["params"] == [value]


@pytest.mark.parametrize(
    "path",
    [
        "tier'); DROP TABLE users;--",
        "$$tier; SELECT 1$$",
        "a,b",
        "유료티어;--",
    ],
)
def test_ast_json_path_keys_are_bound_values(path: str) -> None:
    compiled = compile_ast_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        ast={
            "filter": {
                "jsonb": {"col": "metadata", "path": [path], "cast": None},
                "op": "eq",
                "val": "gold",
            },
        },
    )

    _assert_single_statement_and_values_bound(compiled)
    assert compiled["params"] == [path, "gold"]


@pytest.mark.parametrize("query_params", BAD_IDENTIFIERS)
def test_url_identifier_and_cast_attacks_do_not_emit_sql(query_params: list[tuple[str, str]]) -> None:
    compiled = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=query_params,
    )

    assert compiled["code"] in {"undefined_column", "invalid_cast"}
    assert "sql" not in compiled


@pytest.mark.parametrize(
    "ast",
    [
        {"filter": {"col": "sevverity", "op": "eq", "val": "high"}},
        {"order": [{"col": "title;DROP TABLE users;--"}]},
        {"filter": {"jsonb": {"col": "metadata", "path": ["tier"], "cast": "money"}, "op": "eq", "val": "x"}},
        {"filter": {"or": [{"col": "title", "op": "eq", "val": "safe"}, {"col": "unknown", "op": "eq", "val": "x"}]}},
    ],
)
def test_ast_identifier_and_cast_attacks_do_not_emit_sql(ast: dict[str, Any]) -> None:
    compiled = compile_ast_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        ast=ast,
    )

    assert compiled["code"] in {"undefined_column", "invalid_cast"}
    assert "sql" not in compiled
