"""Unit coverage for table REST route request plumbing."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.datastructures import Headers, QueryParams


class _User:
    user_id = "00000000-0000-0000-0000-000000000001"
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
