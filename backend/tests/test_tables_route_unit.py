"""Unit coverage for table REST route request plumbing."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.datastructures import Headers, QueryParams


class _User:
    user_id = "00000000-0000-0000-0000-000000000001"
    username = "김영로"
    is_admin = False


@pytest.mark.asyncio
async def test_execute_sql_route_streams_envelope_and_forwards_params(monkeypatch) -> None:
    import json

    from starlette.responses import StreamingResponse

    from app.api.routes import tables

    captured: dict[str, Any] = {}

    async def fake_check_vault_access(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"vault_id": "vault-1"}

    async def fake_execute_sql(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"kind": "table_query", "vaults": kwargs["vault_names"], "items": [], "total": 0}

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_service, "execute_sql", fake_execute_sql)

    result = await tables.execute_sql(
        "demo",
        tables.SqlRequest(sql=" SELECT $1::int AS value ", params=[7]),
        _User(),  # type: ignore[arg-type]
    )

    # The route now STREAMS the envelope (so serialising a huge result never
    # blocks the single event loop) — the body is still one ordinary JSON
    # document, byte-for-value identical to the old dict return.
    assert isinstance(result, StreamingResponse)
    assert result.media_type == "application/json"
    body = b"".join([chunk async for chunk in result.body_iterator])
    envelope = json.loads(body)
    assert envelope["kind"] == "table_query"
    assert captured == {
        "vault_names": ["demo"],
        "user_id": _User.user_id,
        "sql": "SELECT $1::int AS value",
        "params": [7],
        "is_admin": False,
    }


@pytest.mark.asyncio
async def test_alter_table_route_forwards_schema_ops_with_writer_gate(monkeypatch) -> None:
    from app.api.routes import tables

    captured: dict[str, Any] = {}
    roles: list[str] = []

    async def fake_check_vault_access(*_args: Any, **kwargs: Any) -> dict[str, str]:
        roles.append(kwargs["required_role"])
        return {"vault_id": "vault-1"}

    async def fake_alter_table(vault_id: str, table_name: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"vault_id": vault_id, "table_name": table_name, **kwargs})
        return {
            "kind": "table",
            "vault": "demo",
            "name": table_name,
            "columns": [{"name": "title", "type": "text"}],
        }

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_service, "alter_table", fake_alter_table)

    result = await tables.alter_table(
        "demo",
        "incidents",
        tables.AlterTableRequest(
            add_columns=[{"name": "title", "type": "text"}],
            alter_columns=[{"name": "title", "set_default": "untitled"}],
            drop_columns=["legacy"],
            rename_columns={"summary": "body"},
            add_unique_keys=[{"columns": ["title"]}],
            drop_unique_keys=["old_title_key"],
            add_indexes=[{"columns": ["title"]}],
            drop_indexes=["old_title_idx"],
        ),
        _User(),  # type: ignore[arg-type]
    )

    assert result["kind"] == "table"
    assert roles == ["writer"]
    assert captured == {
        "vault_id": "vault-1",
        "table_name": "incidents",
        "actor_id": "김영로",
        "add_columns": [{"name": "title", "type": "text"}],
        "alter_columns": [{"name": "title", "set_default": "untitled"}],
        "drop_columns": ["legacy"],
        "rename_columns": {"summary": "body"},
        "add_unique_keys": [{"columns": ["title"]}],
        "drop_unique_keys": ["old_title_key"],
        "add_indexes": [{"columns": ["title"]}],
        "drop_indexes": ["old_title_idx"],
    }


@pytest.mark.asyncio
async def test_table_migration_route_forwards_key_ops_with_writer_gate(monkeypatch) -> None:
    from app.api.routes import tables

    captured: dict[str, Any] = {}
    roles: list[str] = []

    async def fake_check_vault_access(*_args: Any, **kwargs: Any) -> dict[str, str]:
        roles.append(kwargs["required_role"])
        return {"vault_id": "vault-1"}

    async def fake_apply_table_migration(vault_id: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"vault_id": vault_id, **kwargs})
        return {
            "kind": "table_migration",
            "vault": "demo",
            "idempotency_key": kwargs["idempotency_key"],
            "checksum": "abc123",
            "applied": True,
            "operations": len(kwargs["operations"]),
            "results": [],
        }

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_migration_service, "apply_table_migration", fake_apply_table_migration)

    ops = [{"op": "add_column", "table": "incidents", "name": "title", "type": "text"}]
    result = await tables.apply_table_migration(
        "demo",
        ops,
        "11111111-1111-4111-8111-111111111111",
        _User(),  # type: ignore[arg-type]
    )

    assert result["kind"] == "table_migration"
    assert roles == ["writer"]
    assert captured == {
        "vault_id": "vault-1",
        "actor_id": "김영로",
        "idempotency_key": "11111111-1111-4111-8111-111111111111",
        "operations": ops,
    }


@pytest.mark.asyncio
async def test_table_schema_routes_use_reader_gate(monkeypatch) -> None:
    from app.api.routes import tables

    captured: list[tuple[str, tuple[Any, ...]]] = []
    roles: list[str] = []

    async def fake_check_vault_access(*_args: Any, **kwargs: Any) -> dict[str, str]:
        roles.append(kwargs["required_role"])
        return {"vault_id": "vault-1"}

    async def fake_get_table_schema(*args: Any) -> dict[str, Any]:
        captured.append(("table", args))
        return {"kind": "table_schema", "vault": "demo", "name": "incidents"}

    async def fake_get_vault_schema(*args: Any) -> dict[str, Any]:
        captured.append(("vault", args))
        return {"kind": "vault_table_schema", "vault": "demo", "tables": [], "total": 0}

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_schema_service, "get_table_schema", fake_get_table_schema)
    monkeypatch.setattr(tables.table_schema_service, "get_vault_schema", fake_get_vault_schema)

    table_schema = await tables.get_table_schema("demo", "incidents", _User())  # type: ignore[arg-type]
    vault_schema = await tables.get_vault_schema("demo", _User())  # type: ignore[arg-type]

    assert table_schema["kind"] == "table_schema"
    assert vault_schema["kind"] == "vault_table_schema"
    assert roles == ["reader", "reader"]
    assert captured == [
        ("table", ("vault-1", "incidents")),
        ("vault", ("vault-1",)),
    ]


@pytest.mark.asyncio
async def test_select_rows_route_sets_content_range(monkeypatch) -> None:
    from app.api.routes import tables

    captured: dict[str, Any] = {}

    class _Request:
        query_params = QueryParams("severity=eq.high")
        headers = Headers({"prefer": "count=exact", "range": "0-1"})

    class _Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    async def fake_check_vault_access(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"vault_id": "vault-1"}

    async def fake_select_rows(**kwargs: Any) -> tables.table_row_query.RowQueryResponse:
        captured.update(kwargs)
        return tables.table_row_query.RowQueryResponse(
            body={"kind": "table_query", "items": [], "total": 0},
            content_range="0-1/7",
        )

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_row_query, "select_rows", fake_select_rows)

    response = _Response()
    result = await tables.select_rows(
        "demo",
        "incidents",
        _Request(),  # type: ignore[arg-type]
        response,  # type: ignore[arg-type]
        _User(),  # type: ignore[arg-type]
    )

    assert result["kind"] == "table_query"
    assert response.headers["Content-Range"] == "0-1/7"
    assert captured["vault_name"] == "demo"
    assert captured["table_name"] == "incidents"
    assert captured["query_params"] == [("severity", "eq.high")]
    assert captured["range_header"] == "0-1"
    assert captured["prefer_header"] == "count=exact"


@pytest.mark.asyncio
async def test_query_rows_route_forwards_ast_and_headers(monkeypatch) -> None:
    from app.api.routes import tables

    captured: dict[str, Any] = {}

    class _Request:
        headers = Headers({"prefer": "count=exact", "range": "2-3"})

    class _Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    async def fake_check_vault_access(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"vault_id": "vault-1"}

    async def fake_query_rows(**kwargs: Any) -> tables.table_row_query.RowQueryResponse:
        captured.update(kwargs)
        return tables.table_row_query.RowQueryResponse(
            body={"kind": "table_query", "items": [], "total": 0},
            content_range="2-3/7",
        )

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_row_query, "query_rows", fake_query_rows)

    response = _Response()
    result = await tables.query_rows(
        "demo",
        "incidents",
        tables.QueryRowsRequest(
            select=["title"],
            filter={"col": "severity", "op": "eq", "val": "high"},
        ),
        _Request(),  # type: ignore[arg-type]
        response,  # type: ignore[arg-type]
        _User(),  # type: ignore[arg-type]
    )

    assert result["kind"] == "table_query"
    assert response.headers["Content-Range"] == "2-3/7"
    assert captured["vault_name"] == "demo"
    assert captured["table_name"] == "incidents"
    assert captured["ast"] == {
        "select": ["title"],
        "filter": {"col": "severity", "op": "eq", "val": "high"},
    }
    assert captured["range_header"] == "2-3"
    assert captured["prefer_header"] == "count=exact"


@pytest.mark.asyncio
async def test_query_rows_route_dispatches_write_ast_to_writer(monkeypatch) -> None:
    from app.api.routes import tables

    captured: dict[str, Any] = {}
    access_roles: list[str] = []

    class _Request:
        headers = Headers({"prefer": "return=representation"})

    class _Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.status_code: int | None = None

    async def fake_check_vault_access(*_args: Any, **kwargs: Any) -> dict[str, str]:
        access_roles.append(kwargs["required_role"])
        return {"vault_id": "vault-1"}

    async def fake_write_query_rows(**kwargs: Any) -> tables.table_row_write.RowMutationResponse:
        captured.update(kwargs)
        return tables.table_row_write.RowMutationResponse(
            status_code=201,
            body={"kind": "table_query", "items": [{"id": "1"}], "total": 1},
            content_range="0-0/1",
        )

    async def fail_read_query_rows(**_kwargs: Any) -> None:
        raise AssertionError("read query_rows should not handle write AST")

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_row_write, "query_rows", fake_write_query_rows)
    monkeypatch.setattr(tables.table_row_query, "query_rows", fail_read_query_rows)

    response = _Response()
    result = await tables.query_rows(
        "demo",
        "incidents",
        tables.QueryRowsRequest.model_validate(
            {"insert": [{"title": "hello"}], "returning": ["id"]},
        ),
        _Request(),  # type: ignore[arg-type]
        response,  # type: ignore[arg-type]
        _User(),  # type: ignore[arg-type]
    )

    assert result["kind"] == "table_query"
    assert response.status_code == 201
    assert response.headers["Content-Range"] == "0-0/1"
    assert access_roles == ["reader", "writer"]
    assert captured["actor_id"] == "김영로"
    assert captured["ast"] == {"insert": [{"title": "hello"}], "returning": ["id"]}
    assert captured["prefer_header"] == "return=representation"


@pytest.mark.asyncio
async def test_insert_rows_route_forwards_body_query_and_prefer(monkeypatch) -> None:
    from app.api.routes import tables

    captured: dict[str, Any] = {}

    class _Request:
        query_params = QueryParams("select=id,title")
        headers = Headers({"prefer": "return=representation"})

    class _Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.status_code: int | None = None

    async def fake_check_vault_access(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"vault_id": "vault-1"}

    async def fake_insert_rows(**kwargs: Any) -> tables.table_row_write.RowMutationResponse:
        captured.update(kwargs)
        return tables.table_row_write.RowMutationResponse(
            status_code=201,
            body={"kind": "table_query", "items": [{"id": "1"}], "total": 1},
            content_range="0-0/1",
        )

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_row_write, "insert_rows", fake_insert_rows)

    response = _Response()
    result = await tables.insert_rows(
        "demo",
        "incidents",
        _Request(),  # type: ignore[arg-type]
        response,  # type: ignore[arg-type]
        {"title": "hello"},
        _User(),  # type: ignore[arg-type]
    )

    assert result["kind"] == "table_query"
    assert response.status_code == 201
    assert response.headers["Content-Range"] == "0-0/1"
    assert captured["vault_name"] == "demo"
    assert captured["table_name"] == "incidents"
    assert captured["actor_id"] == "김영로"
    assert captured["body"] == {"title": "hello"}
    assert captured["query_params"] == [("select", "id,title")]
    assert captured["prefer_header"] == "return=representation"


@pytest.mark.asyncio
async def test_insert_rows_route_returns_empty_minimal_response(monkeypatch) -> None:
    from app.api.routes import tables

    class _Request:
        query_params = QueryParams("")
        headers = Headers({})

    class _Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.status_code: int | None = None

    async def fake_check_vault_access(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"vault_id": "vault-1"}

    async def fake_insert_rows(**_kwargs: Any) -> tables.table_row_write.RowMutationResponse:
        return tables.table_row_write.RowMutationResponse(
            status_code=204,
            body=None,
            content_range=None,
        )

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_row_write, "insert_rows", fake_insert_rows)

    result = await tables.insert_rows(
        "demo",
        "incidents",
        _Request(),  # type: ignore[arg-type]
        _Response(),  # type: ignore[arg-type]
        {"title": "hello"},
        _User(),  # type: ignore[arg-type]
    )

    assert result.status_code == 204
    assert result.body == b""


@pytest.mark.asyncio
async def test_update_rows_route_returns_empty_minimal_response(monkeypatch) -> None:
    from app.api.routes import tables

    class _Request:
        query_params = QueryParams("severity=eq.high")
        headers = Headers({})

    class _Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.status_code: int | None = None

    async def fake_check_vault_access(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"vault_id": "vault-1"}

    async def fake_update_rows(**_kwargs: Any) -> tables.table_row_write.RowMutationResponse:
        return tables.table_row_write.RowMutationResponse(
            status_code=204,
            body=None,
            content_range="*/5",
        )

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_row_write, "update_rows", fake_update_rows)

    result = await tables.update_rows(
        "demo",
        "incidents",
        _Request(),  # type: ignore[arg-type]
        _Response(),  # type: ignore[arg-type]
        {"severity": "critical"},
        _User(),  # type: ignore[arg-type]
    )

    assert result.status_code == 204
    assert result.headers["Content-Range"] == "*/5"


@pytest.mark.asyncio
async def test_delete_rows_route_forwards_query(monkeypatch) -> None:
    from app.api.routes import tables

    captured: dict[str, Any] = {}

    class _Request:
        query_params = QueryParams("all=true")
        headers = Headers({"prefer": "return=minimal"})

    class _Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    async def fake_check_vault_access(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"vault_id": "vault-1"}

    async def fake_delete_rows(**kwargs: Any) -> tables.table_row_write.RowMutationResponse:
        captured.update(kwargs)
        return tables.table_row_write.RowMutationResponse(
            status_code=204,
            body=None,
            content_range="*/2",
        )

    monkeypatch.setattr(tables, "check_vault_access", fake_check_vault_access)
    monkeypatch.setattr(tables.table_row_write, "delete_rows", fake_delete_rows)

    result = await tables.delete_rows(
        "demo",
        "incidents",
        _Request(),  # type: ignore[arg-type]
        _Response(),  # type: ignore[arg-type]
        _User(),  # type: ignore[arg-type]
    )

    assert result.status_code == 204
    assert captured["vault_name"] == "demo"
    assert captured["table_name"] == "incidents"
    assert captured["query_params"] == [("all", "true")]
    assert captured["prefer_header"] == "return=minimal"
