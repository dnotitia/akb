"""Unit coverage for UserSqlExecutor parameter forwarding and DML shaping."""

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
        if sql.startswith("INSERT"):
            return "INSERT 0 3"
        if sql.startswith("UPDATE"):
            return "UPDATE 5"
        if sql.startswith("DELETE"):
            return "DELETE 2"
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
    assert result == {
        "kind": "table_sql",
        "vaults": [],
        "result": "UPDATE 5",
        "affected_rows": 5,
    }


@pytest.mark.asyncio
async def test_execute_fetch_true_returns_dml_returning_rows() -> None:
    from app.services.user_sql_executor import UserSqlExecutor

    conn = _Conn()
    executor = UserSqlExecutor(cast(Any, _Pool(conn)))

    result = await executor.execute(
        user_id="00000000-0000-0000-0000-000000000001",
        sql="INSERT INTO vt_demo__events (actor) VALUES ($1) RETURNING actor",
        params=["alice"],
        fetch=True,
        is_admin=True,
    )

    assert conn.fetched[-1] == (
        "INSERT INTO vt_demo__events (actor) VALUES ($1) RETURNING actor",
        ("alice",),
    )
    assert all(
        not sql.startswith("INSERT INTO vt_demo__events")
        for sql, _args in conn.executed
    )
    assert result == {
        "kind": "table_query",
        "vaults": [],
        "columns": ["value"],
        "items": [{"value": "alice"}],
        "total": 1,
    }


@pytest.mark.asyncio
async def test_execute_parses_insert_and_delete_command_tags() -> None:
    from app.services.user_sql_executor import UserSqlExecutor

    conn = _Conn()
    executor = UserSqlExecutor(cast(Any, _Pool(conn)))

    insert = await executor.execute(
        user_id="00000000-0000-0000-0000-000000000001",
        sql="INSERT INTO vt_demo__events (actor) VALUES ($1), ($2), ($3)",
        params=["a", "b", "c"],
        is_admin=True,
    )
    delete = await executor.execute(
        user_id="00000000-0000-0000-0000-000000000001",
        sql="DELETE FROM vt_demo__events",
        is_admin=True,
    )

    assert insert["affected_rows"] == 3
    assert delete["affected_rows"] == 2


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
