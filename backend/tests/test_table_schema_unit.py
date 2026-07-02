"""Unit coverage for merged registry/live table schema introspection."""

from app.services import table_service


def test_build_table_schema_serializes_rich_constraints_and_drift() -> None:
    schema = table_service._build_table_schema(
        "demo-vault",
        {
            "name": "incidents",
            "collection": "ops",
            "description": "Incident records",
            "columns": [
                {
                    "name": "status",
                    "type": "text",
                    "required": True,
                    "default": "draft",
                    "check": {"op": "in", "values": ["draft", "open"]},
                    "enum": ["draft", "open"],
                    "references": {"table": "states", "column": "name"},
                    "on_delete": "restrict",
                },
                {"name": "score", "type": "number"},
                {"name": "missing", "type": "boolean"},
            ],
            "unique_keys": [{"name": "incidents_status_key", "columns": ["status"]}],
            "indexes": [
                {
                    "name": "incidents_score_idx",
                    "columns": [{"name": "score", "order": "desc"}],
                }
            ],
        },
        {
            "id": "uuid",
            "created_at": "timestamp with time zone",
            "status": "text",
            "score": "text",
            "stale": "text",
        },
    )

    assert schema["kind"] == "table_schema"
    assert schema["vault"] == "demo-vault"
    assert schema["collection"] == "ops"
    assert schema["name"] == "incidents"
    assert schema["table"] == "incidents"
    assert schema["sql_name"] == "incidents"
    assert schema["unique_keys"] == [{"name": "incidents_status_key", "columns": ["status"]}]
    assert schema["indexes"][0]["columns"] == [{"name": "score", "order": "desc"}]
    assert schema["pg_types"]["score"] == "text"
    assert schema["system_columns"] == ["created_at", "id"]

    status, score, missing = schema["columns"]
    assert status == {
        "name": "status",
        "type": "text",
        "required": True,
        "default": "draft",
        "check": {"op": "in", "values": ["draft", "open"]},
        "enum": ["draft", "open"],
        "unique": True,
        "index": False,
        "references": {"table": "states", "column": "name"},
        "on_delete": "restrict",
        "pg_type": "text",
        "drift": {"missing": False, "type_mismatch": False},
    }
    assert score["unique"] is False
    assert score["index"] is True
    assert score["pg_type"] == "text"
    assert score["drift"] == {"missing": False, "type_mismatch": True}
    assert missing["pg_type"] is None
    assert missing["drift"] == {"missing": True, "type_mismatch": False}

    assert schema["drift"] == {
        "has_drift": True,
        "missing_columns": ["missing"],
        "extra_columns": ["stale"],
        "type_mismatches": [
            {
                "column": "score",
                "registry_type": "number",
                "expected_pg_type": "numeric",
                "pg_type": "text",
            }
        ],
    }
