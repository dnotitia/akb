"""Unit coverage for UserSqlExecutor parameter forwarding."""

from __future__ import annotations

from typing import Any, cast

import pytest


class _Txn:
    async def __aenter__(self) -> "_Txn":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetched: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Txn:
        return _Txn()

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetched.append((sql, args))
        return [{"value": args[0] if args else 1}]


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


@pytest.mark.asyncio
async def test_execute_forwards_params_to_select_fetch() -> None:
    from app.services.user_sql_executor import UserSqlExecutor

    conn = _Conn()
    executor = UserSqlExecutor(cast(Any, _Pool(conn)))

    result = await executor.execute(
        user_id="00000000-0000-0000-0000-000000000001",
        sql="SELECT $1::int AS value",
        params=[7],
        is_admin=True,
    )

    assert conn.fetched[-1] == ("SELECT $1::int AS value", (7,))
    assert result == {
        "kind": "table_query",
        "vaults": [],
        "columns": ["value"],
        "items": [{"value": 7}],
        "total": 1,
    }


@pytest.mark.asyncio
async def test_execute_forwards_params_to_non_select_execute() -> None:
    from app.services.user_sql_executor import UserSqlExecutor

    conn = _Conn()
    executor = UserSqlExecutor(cast(Any, _Pool(conn)))

    result = await executor.execute(
        user_id="00000000-0000-0000-0000-000000000001",
        sql="UPDATE vt_demo__events SET actor = $1",
        params=["alice"],
        is_admin=True,
    )

    assert conn.executed[-1] == ("UPDATE vt_demo__events SET actor = $1", ("alice",))
    assert result == {"kind": "table_sql", "vaults": [], "result": "OK"}


@pytest.mark.asyncio
async def test_execute_treats_empty_params_as_no_bindings() -> None:
    from app.services.user_sql_executor import UserSqlExecutor

    conn = _Conn()
    executor = UserSqlExecutor(cast(Any, _Pool(conn)))

    result = await executor.execute(
        user_id="00000000-0000-0000-0000-000000000001",
        sql="SELECT 1 AS value",
        params=[],
        is_admin=True,
    )

    assert conn.fetched[-1] == ("SELECT 1 AS value", ())
    assert result["items"] == [{"value": 1}]
