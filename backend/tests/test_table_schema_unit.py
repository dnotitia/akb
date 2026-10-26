"""Unit coverage for merged registry/live table schema introspection."""

import pytest
from jsonschema import Draft202012Validator

from app.openapi_contract import JSON_OBJECT_SCHEMA, JSON_VALUE_SCHEMA, _success_envelope_schemas
from app.services import table_schema_service


def test_build_table_schema_serializes_rich_constraints_and_drift() -> None:
    schema = table_schema_service._build_table_schema(
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
    assert "enum" not in score
    assert score["index"] is True
    assert score["pg_type"] == "text"
    assert score["drift"] == {"missing": False, "type_mismatch": True}
    assert missing["pg_type"] is None
    assert "enum" not in missing
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


@pytest.mark.parametrize("extra", [{}, {"enum": None}, {"enum": ["draft", "open"]}, {"enum": []}])
@pytest.mark.parametrize("required", [False, True])
def test_enum_projection_matches_the_published_contract(extra, required) -> None:
    schema = table_schema_service._build_table_schema(
        "demo-vault",
        {"name": "items", "columns": [{"name": "value", "type": "text", "required": required, **extra}]},
        {"id": "uuid", "value": "text"},
    )
    components = {
        **_success_envelope_schemas(),
        "AkbJsonValue": JSON_VALUE_SCHEMA,
        "AkbJsonObject": JSON_OBJECT_SCHEMA,
    }
    for name, payload in (
        ("AkbTableSchemaEnvelope", schema),
        ("AkbVaultTableSchemaEnvelope", {
            "kind": "vault_table_schema", "vault": "demo-vault", "tables": [schema], "total": 1,
        }),
    ):
        Draft202012Validator({
            "$ref": f"#/components/schemas/{name}", "components": {"schemas": components},
        }).validate(payload)

    column = schema["columns"][0]
    assert column["required"] is required
    if extra.get("enum") is None:
        assert "enum" not in column
    else:
        # Preserve supplied arrays, including legacy empty arrays; do not
        # reinterpret an empty set as an unconstrained column.
        assert column["enum"] == extra["enum"]
