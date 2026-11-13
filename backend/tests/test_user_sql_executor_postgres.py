"""Prove SQL transaction and role state do not leak across pooled calls."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from app.services.role_sync import user_role_name
from app.services.user_sql_executor import PermissionDeniedError, UserSqlExecutor
from tests.test_events_commit_order_postgres import _database_dsn, _DSN, _fresh_database


@pytest.mark.asyncio
async def test_reused_connection_resets_read_only_and_role_after_success_and_failure():
    user_id = uuid.uuid4()
    role = user_role_name(user_id)
    admin = None
    try:
        async with _fresh_database() as fixture_pool:
            admin = await asyncpg.connect(_DSN)
            async with fixture_pool.acquire() as conn:
                database = await conn.fetchval("SELECT current_database()")
                await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
                await conn.execute("CREATE TABLE executor_pool_test (value text NOT NULL)")
                await conn.execute("INSERT INTO executor_pool_test VALUES ('original')")
                await conn.execute(f'GRANT SELECT, UPDATE ON executor_pool_test TO "{role}"')

            pool = await asyncpg.create_pool(_database_dsn(database), min_size=1, max_size=1)
            try:
                executor = UserSqlExecutor(pool)
                async with pool.acquire() as conn:
                    pid = await conn.fetchval("SELECT pg_backend_pid()")
                    default_role = await conn.fetchval("SELECT current_user")

                async def execute(sql: str, *, read_only: bool):
                    return await executor.execute(
                        user_id=user_id, actor_id="pool-test", sql=sql, read_only=read_only,
                    )

                async def assert_clean(expected: str):
                    async with pool.acquire() as conn:
                        assert await conn.fetchval("SELECT pg_backend_pid()") == pid
                        assert await conn.fetchval("SELECT current_user") == default_role
                        assert await conn.fetchval("SHOW transaction_read_only") == "off"
                        assert await conn.fetchval("SELECT NULLIF(current_setting('akb.actor_id', true), '')") is None
                        assert await conn.fetchval("SELECT value FROM executor_pool_test") == expected

                write = await execute("UPDATE executor_pool_test SET value = 'first'", read_only=False)
                assert write["affected_rows"] == 1
                await assert_clean("first")
                read = await execute(
                    "SELECT pg_backend_pid() AS pid, current_user AS role, "
                    "current_setting('transaction_read_only') AS mode, value FROM executor_pool_test",
                    read_only=True,
                )
                assert read["items"] == [{"pid": pid, "role": role, "mode": "on", "value": "first"}]
                await assert_clean("first")
                write = await execute("UPDATE executor_pool_test SET value = 'after_read'", read_only=False)
                assert write["affected_rows"] == 1
                await assert_clean("after_read")
                with pytest.raises(PermissionDeniedError) as denied:
                    await execute("UPDATE executor_pool_test SET value = 'blocked'", read_only=True)
                assert denied.value.pg_sqlstate == "25006"
                await assert_clean("after_read")
                write = await execute("UPDATE executor_pool_test SET value = 'second'", read_only=False)
                assert write["affected_rows"] == 1
                await assert_clean("second")
                read = await execute("SELECT value FROM executor_pool_test", read_only=True)
                assert read["items"] == [{"value": "second"}]
                await assert_clean("second")
            finally:
                await pool.close()
    finally:
        if admin is not None:
            await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            await admin.close()
