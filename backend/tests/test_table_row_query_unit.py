"""Unit coverage for row-read URL query compilation."""

from __future__ import annotations

from decimal import Decimal

from app.services.table_row_query import compile_row_query


COLUMNS = [
    {"name": "title", "type": "text"},
    {"name": "severity", "type": "text"},
    {"name": "score", "type": "number"},
    {"name": "metadata", "type": "json"},
]


def test_compile_select_filter_order_count_and_page() -> None:
    compiled = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[
            ("select", "id,title,severity"),
            ("severity", "in.(high,critical)"),
            ("order", "created_at.desc"),
            ("limit", "2"),
        ],
        prefer_header="count=exact",
    )

    assert "error" not in compiled
    assert compiled["sql"] == (
        "WITH __akb_count AS (SELECT count(*) AS __akb_total FROM vt_eng__incidents "
        "WHERE (severity = ANY($1))), __akb_page AS (SELECT id, title, severity, "
        "TRUE AS __akb_present FROM vt_eng__incidents WHERE (severity = ANY($1)) "
        "ORDER BY created_at DESC LIMIT 2 OFFSET 0) SELECT __akb_page.*, "
        "__akb_count.__akb_total FROM __akb_count LEFT JOIN __akb_page ON TRUE"
    )
    assert compiled["params"] == [["high", "critical"]]


def test_compile_json_path_cast_uses_bound_key_and_value() -> None:
    compiled = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[
            ("select", "metadata->>tier,metadata#>>{stats,count}::int"),
            ("metadata->>tier", "eq.gold"),
            ("metadata#>>{stats,count}::int", "gt.5"),
            ("order", "metadata->>tier.desc"),
        ],
    )

    assert "error" not in compiled
    assert compiled["sql"] == (
        "SELECT metadata ->> $1::text AS __akb_col_0, "
        "(metadata #>> $2::text[])::integer AS __akb_col_1 "
        "FROM vt_eng__incidents WHERE (metadata ->> $3::text = $4) "
        "AND ((metadata #>> $5::text[])::integer > $6) "
        "ORDER BY metadata ->> $7::text DESC LIMIT 100 OFFSET 0"
    )
    assert compiled["params"] == [
        "tier",
        ["stats", "count"],
        "tier",
        "gold",
        ["stats", "count"],
        5,
        "tier",
    ]


def test_compile_boolean_group_and_numeric_conversion() -> None:
    compiled = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("or", "(severity.eq.high,score.gte.0.9)")],
    )

    assert "error" not in compiled
    assert compiled["sql"] == (
        "SELECT * FROM vt_eng__incidents "
        "WHERE ((severity = $1) OR (score >= $2)) LIMIT 100 OFFSET 0"
    )
    assert compiled["params"] == ["high", Decimal("0.9")]


def test_compile_json_containment_object_as_json_param() -> None:
    compiled = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("metadata", 'cs.{"tier":"gold"}')],
    )

    assert "error" not in compiled
    assert compiled["sql"] == (
        "SELECT * FROM vt_eng__incidents WHERE (metadata @> $1::jsonb) LIMIT 100 OFFSET 0"
    )
    assert compiled["params"] == ['{"tier": "gold"}']


def test_compile_rejects_unknown_identifier_operator_and_cast() -> None:
    unknown_column = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("sevverity", "eq.high")],
    )
    assert unknown_column["code"] == "undefined_column"

    unknown_operator = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("severity", "fts.high")],
    )
    assert unknown_operator["code"] == "invalid_operator"

    unknown_cast = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("metadata->>count::money", "gt.1")],
    )
    assert unknown_cast["code"] == "invalid_cast"


def test_compile_rejects_excessive_boolean_depth() -> None:
    result = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("or", "(and(or(and(severity.eq.high))))")],
    )

    assert result["code"] == "filter_too_deep"


def test_compile_rejects_empty_boolean_group() -> None:
    top_level = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("or", "()")],
    )
    assert top_level["code"] == "invalid_filter"

    nested = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("and", "(or())")],
    )
    assert nested["code"] == "invalid_filter"


def test_range_header_overrides_limit_offset_and_clamps() -> None:
    compiled = compile_row_query(
        vault_name="eng",
        table_name="incidents",
        columns=COLUMNS,
        query_params=[("limit", "5"), ("offset", "10")],
        range_header="20-2000",
    )

    assert "error" not in compiled
    assert compiled["sql"].endswith("LIMIT 1000 OFFSET 20")


def test_empty_exact_count_page_shapes_total_without_item() -> None:
    from app.services.table_row_query import RowQueryResponse, _shape_result

    body, content_range = _shape_result(
        {
            "items": [{"__akb_present": None, "__akb_total": 7, "title": None}],
            "columns": ["title", "__akb_present", "__akb_total"],
        },
        vault_name="eng",
        table_name="incidents",
        projections=[],
        count_exact=True,
        offset=100,
    )

    assert RowQueryResponse(body=body, content_range=content_range).content_range == "*/7"
    assert body["total"] == 7
    assert body["items"] == []
    assert body["columns"] == []
