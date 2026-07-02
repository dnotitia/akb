"""Unit coverage for row-write SQL compilation."""

from __future__ import annotations

import json

from app.services.table_row_write import (
    compile_delete_rows,
    compile_insert_rows,
    compile_update_rows,
)


COLUMNS = [
    {"name": "title", "type": "text"},
    {"name": "severity", "type": "text"},
    {"name": "metadata", "type": "json"},
]


def test_compile_insert_bulk_uses_union_columns_defaults_and_server_actor() -> None:
    compiled = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        actor_id="alice",
        body=[
            {
                "title": "a",
                "severity": "high",
                "created_by": "mallory",
                "updated_at": "2099-01-01T00:00:00Z",
            },
            {"title": "b"},
        ],
        query_params=[("select", "id,title,severity")],
        prefer_header="return=representation",
    )

    assert not isinstance(compiled, dict)
    assert compiled.fetch is True
    assert compiled.status_code == 201
    assert compiled.sql == (
        "INSERT INTO vt_eng__incidents (title, severity, created_by) "
        "VALUES ($1, $2, $3), ($4, DEFAULT, $5) RETURNING id, title, severity"
    )
    assert compiled.params == ["a", "high", "alice", "b", "alice"]


def test_compile_insert_allows_client_id_and_created_at() -> None:
    compiled = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        actor_id="alice",
        body={
            "id": "00000000-0000-0000-0000-000000000001",
            "created_at": "2026-07-02T00:00:00Z",
            "title": "imported",
        },
    )

    assert not isinstance(compiled, dict)
    assert compiled.sql == (
        "INSERT INTO vt_eng__incidents (id, created_at, title, created_by) "
        "VALUES ($1, $2, $3, $4)"
    )
    assert compiled.params == [
        "00000000-0000-0000-0000-000000000001",
        "2026-07-02T00:00:00Z",
        "imported",
        "alice",
    ]


def test_compile_insert_rejects_unknown_columns_and_bulk_overflow() -> None:
    unknown = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        actor_id="alice",
        body={"sevverity": "high"},
    )
    assert isinstance(unknown, dict)
    assert unknown["code"] == "undefined_column"

    overflow = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        actor_id="alice",
        body=[{"title": str(i)} for i in range(1001)],
    )
    assert isinstance(overflow, dict)
    assert overflow["code"] == "bulk_too_large"

    upsert = compile_insert_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        actor_id="alice",
        body={"id": "00000000-0000-0000-0000-000000000001", "title": "a"},
        query_params=[("on_conflict", "id")],
    )
    assert isinstance(upsert, dict)
    assert upsert["code"] == "method_not_allowed"


def test_compile_update_reuses_filters_and_ignores_server_columns() -> None:
    compiled = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={
            "severity": "critical",
            "metadata": {"tier": "gold"},
            "id": "00000000-0000-0000-0000-000000000001",
            "created_by": "mallory",
        },
        query_params=[("severity", "eq.high")],
        prefer_header="return=representation",
    )

    assert not isinstance(compiled, dict)
    assert compiled.fetch is True
    assert compiled.status_code == 200
    assert compiled.sql == (
        "UPDATE vt_eng__incidents SET severity = $1, metadata = $2, "
        "updated_at = NOW() WHERE (severity = $3) RETURNING *"
    )
    assert compiled.params == ["critical", json.dumps({"tier": "gold"}), "high"]


def test_compile_update_requires_filter_or_all_true() -> None:
    unfiltered = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={"severity": "critical"},
    )
    assert isinstance(unfiltered, dict)
    assert unfiltered["code"] == "unfiltered_mutation"

    all_rows = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={"severity": "critical"},
        query_params=[("all", "true")],
    )
    assert not isinstance(all_rows, dict)
    assert all_rows.sql == (
        "UPDATE vt_eng__incidents SET severity = $1, updated_at = NOW() WHERE TRUE"
    )
    assert all_rows.fetch is False
    assert all_rows.status_code == 204

    read_only_param = compile_update_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        body={"severity": "critical"},
        query_params=[("order", "id.asc")],
    )
    assert isinstance(read_only_param, dict)
    assert read_only_param["code"] == "unfiltered_mutation"


def test_compile_delete_requires_filter_and_can_return_representation() -> None:
    unfiltered = compile_delete_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
    )
    assert isinstance(unfiltered, dict)
    assert unfiltered["code"] == "unfiltered_mutation"

    read_only_param = compile_delete_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("limit", "1")],
    )
    assert isinstance(read_only_param, dict)
    assert read_only_param["code"] == "unfiltered_mutation"

    compiled = compile_delete_rows(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("severity", "eq.low"), ("select", "id,title")],
        prefer_header="return=representation",
    )
    assert not isinstance(compiled, dict)
    assert compiled.fetch is True
    assert compiled.status_code == 200
    assert compiled.sql == (
        "DELETE FROM vt_eng__incidents WHERE (severity = $1) RETURNING id, title"
    )
    assert compiled.params == ["low"]
