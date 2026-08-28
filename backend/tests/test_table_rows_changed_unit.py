"""Focused contract checks for dynamic-table row-change event wiring."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.repositories import table_data_repo


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"


@pytest.mark.asyncio
async def test_new_dynamic_table_installs_statement_row_change_triggers() -> None:
    conn = _Conn()

    await table_data_repo.create_dynamic_table(
        conn,
        "vt_demo__orders",
        [{"name": "value", "type": "text"}],
        vault_name="demo",
        vault_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        resource_uri="akb://demo/table/orders",
    )

    ddl = "\n".join(sql for sql, _args in conn.executed)
    assert "akb_dynamic_table_rows_changed" in ddl
    assert "REFERENCING NEW TABLE AS akb_rows_changed_new" in ddl
    assert "REFERENCING OLD TABLE AS akb_rows_changed_old" in ddl
    assert "FOR EACH STATEMENT" in ddl


@pytest.mark.asyncio
async def test_user_sql_executor_accepts_actor_context() -> None:
    from app.services.user_sql_executor import UserSqlExecutor

    class _Txn:
        async def __aenter__(self) -> "_Txn":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _Pool:
        def acquire(self) -> Any:
            class _Acquire:
                async def __aenter__(self) -> _Conn:
                    return conn

                async def __aexit__(self, *exc: object) -> bool:
                    return False

            return _Acquire()

    conn = _Conn()
    conn.transaction = lambda: _Txn()  # type: ignore[method-assign]
    executor = UserSqlExecutor(_Pool())

    await executor.execute(
        user_id="user-id",
        actor_id="actor-name",
        sql="UPDATE vt_demo__orders SET value = 'next'",
        is_admin=True,
    )

    assert any(
        "set_config('akb.actor_id'" in sql and args == ("actor-name",)
        for sql, args in conn.executed
    )
