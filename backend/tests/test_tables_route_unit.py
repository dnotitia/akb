"""Unit coverage for table REST route request plumbing."""

from __future__ import annotations

from typing import Any

import pytest


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
