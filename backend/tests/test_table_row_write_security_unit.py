"""Security invariants for the row-write compiler.

The compiler may emit table/column identifiers after registry validation, but
caller-controlled values must only appear as asyncpg parameters. These tests
extend the row-read compiler security checks to INSERT/UPDATE/DELETE/UPSERT and
write-AST frontends.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.repositories.table_data_repo import count_statement_separators
from app.services.table_row_write import (
    compile_ast_mutation,
    compile_delete_rows,
    compile_insert_rows,
    compile_update_rows,
)


COLUMNS = [
    {"name": "title", "type": "text"},
    {"name": "severity", "type": "text"},
    {"name": "external_id", "type": "text"},
    {"name": "metadata", "type": "json"},
]
UNIQUE_KEYS = [{"name": "incidents_external_id_key", "columns": ["external_id"]}]
ADVERSARIAL_VALUES = [
    "x'; DROP TABLE users;--",
    "$$x; SELECT pg_read_file('/etc/passwd'); $$",
    "x/*comment*/; SELECT 1",
    "한글'; --",
    "$1 OR TRUE",
    "a,b)c",
]


def _flatten(values: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if isinstance(value, list):
            out.extend(_flatten(value))
        else:
            out.append(value)
    return out


def _assert_single_statement_and_values_bound(compiled: Any) -> None:
    assert not isinstance(compiled, dict)
    assert count_statement_separators(compiled.sql) == 0
    for value in _flatten(compiled.params):
        if isinstance(value, str) and value and value.startswith("__akb_payload_"):
            assert value not in compiled.sql


@settings(max_examples=60, deadline=None)
@given(value=st.text(min_size=1, max_size=30).map(lambda s: f"__akb_payload_{s}__"))
def test_insert_values_are_always_bound(value: str) -> None:
    compiled = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        actor_id="alice",
        body={"title": value, "metadata": {"payload": value}},
        prefer_header="return=representation",
    )

    _assert_single_statement_and_values_bound(compiled)
    assert not isinstance(compiled, dict)
    assert value in compiled.params


@settings(max_examples=60, deadline=None)
@given(value=st.text(min_size=1, max_size=30).map(lambda s: f"__akb_payload_{s}__"))
def test_update_set_values_are_always_bound(value: str) -> None:
    compiled = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={"title": value},
        query_params=[("severity", "eq.high")],
        prefer_header="return=representation",
    )

    _assert_single_statement_and_values_bound(compiled)
    assert not isinstance(compiled, dict)
    assert compiled.params == [value, "high"]


@settings(max_examples=60, deadline=None)
@given(value=st.text(min_size=1, max_size=30).map(lambda s: f"__akb_payload_{s}__"))
def test_upsert_excluded_values_are_always_bound(value: str) -> None:
    compiled = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        unique_keys=UNIQUE_KEYS,
        actor_id="alice",
        body={"external_id": "INC-1", "title": value},
        query_params=[("on_conflict", "external_id")],
        prefer_header="return=representation",
    )

    _assert_single_statement_and_values_bound(compiled)
    assert not isinstance(compiled, dict)
    assert value in compiled.params


@pytest.mark.parametrize("value", ADVERSARIAL_VALUES)
def test_curated_adversarial_values_are_bound_across_write_frontends(value: str) -> None:
    marker = f"__akb_payload_{value}__"
    insert = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        actor_id="alice",
        body={"title": marker},
    )
    update = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={"title": marker},
        query_params=[("severity", "eq.high")],
    )
    ast = compile_ast_mutation(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        ast={
            "update": {"title": marker},
            "where": {"col": "severity", "op": "eq", "val": marker},
            "returning": "*",
        },
        actor_id="alice",
    )

    _assert_single_statement_and_values_bound(insert)
    _assert_single_statement_and_values_bound(update)
    _assert_single_statement_and_values_bound(ast)


@pytest.mark.parametrize(
    "body",
    [
        {"title;DROP TABLE users;--": "safe"},
        {"metadata->>tier": "safe"},
        {"unknown": "safe"},
    ],
)
def test_insert_identifier_attacks_do_not_emit_sql(body: dict[str, str]) -> None:
    compiled = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        actor_id="alice",
        body=body,
    )

    assert isinstance(compiled, dict)
    assert compiled["code"] == "undefined_column"
    assert "sql" not in compiled


@pytest.mark.parametrize("target", ["external_id); DROP TABLE users;--", "severity"])
def test_upsert_target_attacks_do_not_emit_sql(target: str) -> None:
    compiled = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        unique_keys=UNIQUE_KEYS,
        actor_id="alice",
        body={"external_id": "INC-1", "title": "safe"},
        query_params=[("on_conflict", target)],
    )

    assert isinstance(compiled, dict)
    assert compiled["code"] in {"undefined_column", "no_unique_constraint"}
    assert "sql" not in compiled


def test_read_only_params_do_not_bypass_unfiltered_guard() -> None:
    update = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={"title": "unsafe"},
        query_params=[("order", "id.asc"), ("limit", "1")],
    )
    delete = compile_delete_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("offset", "1")],
    )

    assert isinstance(update, dict)
    assert update["code"] == "unfiltered_mutation"
    assert isinstance(delete, dict)
    assert delete["code"] == "unfiltered_mutation"
