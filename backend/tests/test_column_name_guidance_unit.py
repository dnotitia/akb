"""A refused column name is refused once, completely, and with the way out (akb#433).

Column names are SQL identifiers: `akb_sql` runs against the physical table, so
the name an agent writes in SQL is the name the registry holds. That rule stays.
What changes is what a refusal tells the caller, and whether an agent can later
see the header it had to leave behind.

A table taken from a document typically has several headers the rule refuses.
The refusal used to stop at the first one, name a regex, and say nothing about
where the header could go — so an agent learned the rule one column per call.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import pytest

from app.exceptions import ValidationError
from app.services import table_service


def _refusal(columns: list[dict]) -> str:
    with pytest.raises(ValidationError) as caught:
        table_service._normalize_column_specs(columns)
    return str(caught.value)


def test_every_refused_column_is_named_in_one_error():
    message = _refusal([
        {"name": "분류", "type": "text"},
        {"name": "fine_column", "type": "text"},
        {"name": "TriviaQA(비과학 문헌)", "type": "text"},
        {"name": "id", "type": "text"},
        {"name": "Score", "type": "number"},
    ])

    for refused in ("분류", "TriviaQA(비과학 문헌)", "id", "Score"):
        assert repr(refused) in message
    assert "fine_column" not in message
    assert message.startswith("4 column names cannot be used")


def test_each_refusal_says_why_and_where_the_header_goes():
    message = _refusal([
        {"name": "분류", "type": "text"},
        {"name": "id", "type": "text"},
        {"name": "Score", "type": "text"},
        {"name": "1st_place", "type": "text"},
        {"name": "unit price", "type": "text"},
    ])

    assert "'분류' is not ASCII" in message
    # The shell suite looks for this word on a reserved-name refusal.
    assert "'id' is reserved" in message
    assert "'Score' has uppercase letters" in message
    assert "'1st_place' does not start with a letter" in message
    assert "'unit price' contains characters other than a-z, 0-9 and _" in message
    # The way out, not only the rule.
    assert "description" in message
    assert "akb_sql" in message


def test_a_name_postgres_would_cut_is_refused():
    """PostgreSQL keeps 63 bytes of an identifier and drops the rest, so the
    registry and the physical column would name different things."""
    message = _refusal([{"name": "a" * 64, "type": "text"}])
    assert "longer than 63 bytes" in message

    kept = table_service._normalize_column_specs([{"name": "a" * 63, "type": "text"}])
    assert kept[0]["name"] == "a" * 63


def test_alter_refuses_every_bad_name_at_once():
    with pytest.raises(ValidationError) as caught:
        table_service._refuse_bad_column_names(["분류", "category", "Score"])
    message = str(caught.value)
    assert message.startswith("2 column names cannot be used")
    assert "'category'" not in message


def test_a_description_survives_normalization():
    kept = table_service._normalize_column_specs(
        [{"name": "category", "type": "text", "description": "분류"}],
    )
    assert kept[0]["description"] == "분류"


def test_the_tools_tell_agents_where_the_header_goes():
    from mcp_server.tools import TOOLS

    by_name = {tool.name: tool for tool in TOOLS}
    create = by_name["akb_create_table"].input_schema["properties"]["columns"]["items"]["properties"]
    add = by_name["akb_alter_table"].input_schema["properties"]["add_columns"]["items"]["properties"]

    for column in (create, add):
        assert "description" in column
        assert "header" in column["description"]["description"]
        assert "akb_sql" in column["name"]["description"]


class _Conn:
    """Answers the three reads `_list_tables_with_schema` makes."""

    def __init__(self, registry: list[dict], attributes: list[dict]):
        self._registry = registry
        self._attributes = attributes

    async def fetch(self, sql: str, *args):
        if "FROM vault_tables" in sql:
            return self._registry
        if "pg_attribute" in sql:
            return self._attributes
        raise AssertionError(f"unexpected query: {sql}")

    async def fetchval(self, sql: str, *args):
        return 0  # no rows, so no sample row is read


class _Pool:
    def __init__(self, conn: _Conn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


async def test_vault_info_shows_each_columns_description(monkeypatch):
    """akb_vault_info is where an agent reads a schema before writing SQL."""
    from app.repositories.table_data_repo import pg_table_name
    from app.services import access_service

    physical = pg_table_name("docs", "items")
    conn = _Conn(
        registry=[{
            "id": uuid.uuid4(), "name": "items",
            "unique_keys": "[]", "indexes": "[]",
            "columns": json.dumps([
                {"name": "category", "type": "text", "description": "분류"},
                {"name": "score", "type": "number"},
            ]),
        }],
        attributes=[
            {"table_name": physical, "name": "id", "type": "uuid", "attnum": 1},
            {"table_name": physical, "name": "category", "type": "text", "attnum": 2},
            {"table_name": physical, "name": "score", "type": "numeric", "attnum": 3},
        ],
    )

    async def get_pool():
        return _Pool(conn)

    monkeypatch.setattr(access_service, "get_pool", get_pool)
    tables = await access_service._list_tables_with_schema("docs", uuid.uuid4())

    columns = {c["name"]: c for c in tables[0]["columns"]}
    assert columns["category"]["description"] == "분류"
    assert "description" not in columns["score"]
    assert "description" not in columns["id"]
