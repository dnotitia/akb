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
async def test_execute_sql_route_forwards_params(monkeypatch) -> None:
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

    assert result["kind"] == "table_query"
    assert captured == {
        "vault_names": ["demo"],
        "user_id": _User.user_id,
        "sql": "SELECT $1::int AS value",
        "params": [7],
        "is_admin": False,
    }


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
