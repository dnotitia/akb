"""DB-free unit coverage for REST table migrations (AKB-068)."""

from __future__ import annotations

import pytest

from app.exceptions import ValidationError
from app.services import table_migration_service


def test_table_migration_checksum_is_stable_for_json_key_order() -> None:
    ops_a = [
        {
            "op": "add_column",
            "table": "incidents",
            "name": "status",
            "type": "text",
            "default": "todo",
        }
    ]
    ops_b = [
        {
            "type": "text",
            "default": "todo",
            "name": "status",
            "table": "incidents",
            "op": "add_column",
        }
    ]

    assert table_migration_service.table_migration_checksum(ops_a) == table_migration_service.table_migration_checksum(ops_b)


def test_table_migration_operations_are_nfc_normalized_before_checksum() -> None:
    ops_nfd = [
        {
            "op": "add_column",
            "table": "cafe\u0301",
            "name": "memo",
            "type": "text",
            "default": "e\u0301",
        }
    ]
    ops_nfc = [
        {
            "op": "add_column",
            "table": "café",
            "name": "memo",
            "type": "text",
            "default": "é",
        }
    ]

    assert table_migration_service.table_migration_checksum(ops_nfd) == table_migration_service.table_migration_checksum(ops_nfc)
    normalized = table_migration_service._normalize_migration_operations(ops_nfd)
    table, kwargs = table_migration_service._migration_op_to_alter_kwargs(normalized[0])
    assert table == "café"
    assert kwargs["add_columns"][0]["default"] == "é"


def test_table_migration_op_routes_to_alter_kwargs() -> None:
    table, kwargs = table_migration_service._migration_op_to_alter_kwargs({
        "op": "add_column",
        "table": "incidents",
        "name": "status",
        "type": "enum",
        "enum": ["todo", "done"],
        "default": "todo",
        "index": True,
    })

    assert table == "incidents"
    assert kwargs == {
        "add_columns": [
            {
                "name": "status",
                "type": "enum",
                "enum": ["todo", "done"],
                "default": "todo",
                "index": True,
            }
        ]
    }

    table, kwargs = table_migration_service._migration_op_to_alter_kwargs({
        "op": "rename_column",
        "table_name": "incidents",
        "from": "status",
        "to": "state",
    })
    assert table == "incidents"
    assert kwargs == {"rename_columns": {"status": "state"}}

    table, kwargs = table_migration_service._migration_op_to_alter_kwargs({
        "op": "alter_column",
        "table": "incidents",
        "column": {"name": "state", "set_default": "done", "drop_check": True},
    })
    assert table == "incidents"
    assert kwargs == {"alter_columns": [{"name": "state", "set_default": "done", "drop_check": True}]}


def test_table_migration_rejects_unknown_ops_and_bad_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        table_migration_service._migration_op_to_alter_kwargs({"op": "sql", "table": "incidents"})

    with pytest.raises(ValidationError):
        table_migration_service._validate_idempotency_key(None)

    with pytest.raises(ValidationError):
        table_migration_service._validate_idempotency_key("not-a-uuid")

    assert (
        table_migration_service._validate_idempotency_key("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")
        == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
