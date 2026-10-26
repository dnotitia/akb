"""Real asyncpg cancellation, rollback, connection loss and pool recovery.

Every case owns a disposable database/role; no existing application data is
changed. Set AKB_TEST_DSN (or AKB_VCHORD_TEST_DSN) to a test-cluster admin DSN.
"""

import asyncio
from contextlib import asynccontextmanager
import os
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import asyncpg
from fastapi import FastAPI
import pytest

from app.exceptions import TransientDependencyError
from app.http_middleware import CancelDisconnectedReadMiddleware, NoStorePublicSurfacesMiddleware
from app.services.user_sql_executor import UserSqlExecutor, user_role_name


pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _runtime(*, statement_timeout="0", command_timeout=3):
    dsn = os.environ.get("AKB_TEST_DSN") or os.environ.get("AKB_VCHORD_TEST_DSN")
    if not dsn:
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("A test-cluster DSN is required")
        pytest.skip("A test-cluster DSN is required")
    name = f"akb_transient_{uuid.uuid4().hex[:12]}"
    uid = uuid.uuid4()
    role = user_role_name(uid)
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        await admin.execute(f'CREATE ROLE "{role}" NOLOGIN')
        pool = await asyncpg.create_pool(
            urlunsplit(urlsplit(dsn)._replace(path=f"/{name}")),
            min_size=1, max_size=1, command_timeout=command_timeout,
            server_settings={"statement_timeout": statement_timeout},
        )
        async with pool.acquire() as conn:
            await conn.execute("CREATE TABLE fixture(id int PRIMARY KEY, value int NOT NULL)")
            await conn.execute("INSERT INTO fixture VALUES (1, 0)")
            await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            await conn.execute(f'GRANT SELECT, UPDATE ON fixture TO "{role}"')
        executor = UserSqlExecutor(pool)

        async def execute(sql):
            return await executor.execute(user_id=uid, actor_id="recovery-test", sql=sql)

        yield admin, pool, name, execute
    finally:
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), 2)
            except TimeoutError:
                pool.terminate()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


async def _active(admin, database, marker):
    return await admin.fetchval(
        "SELECT pid FROM pg_stat_activity WHERE datname=$1 AND state='active' "
        "AND position($2 in query)>0", database, marker,
    )


async def _wait_active(admin, database, marker):
    async with asyncio.timeout(2):
        while (pid := await _active(admin, database, marker)) is None:
            await asyncio.sleep(0.01)
    return pid


async def _assert_clean(pool, *, value=0):
    async with pool.acquire(timeout=0.5) as conn:
        assert await conn.fetchval("SELECT current_user = session_user")
        assert await conn.fetchval("SHOW role") == "none"
        assert await conn.fetchval("SELECT value FROM fixture WHERE id=1") == value
        assert await conn.fetchval("SELECT 1") == 1
    assert pool.get_idle_size() == 1


@pytest.mark.parametrize("timeout_kind", ["statement", "command"])
async def test_actual_deadline_rolls_back_write_and_pool_is_reusable(timeout_kind):
    async with _runtime(statement_timeout="100ms" if timeout_kind == "statement" else "0",
                        command_timeout=3 if timeout_kind == "statement" else 0.1) as (_, pool, _, execute):
        started = time.monotonic()
        with pytest.raises(TransientDependencyError) as caught:
            await execute("WITH changed AS (UPDATE fixture SET value=value+1 WHERE id=1 RETURNING value) "
                          "SELECT value, pg_sleep(5) FROM changed")
        assert caught.value.code == "operation_timeout"
        assert time.monotonic() - started < 1
        await _assert_clean(pool)


async def test_actual_connection_loss_returns_transient_and_reconnects():
    async with _runtime() as (admin, pool, database, execute):
        marker = "connection_loss_probe"
        task = asyncio.create_task(execute(f"SELECT pg_sleep(10) /* {marker} */"))
        try:
            pid = await _wait_active(admin, database, marker)
            # An external admin terminates the actual connection, just as on
            # a restart. The confined user role cannot terminate superuser
            # sessions and must not be given that capability for this test.
            assert await admin.fetchval("SELECT pg_terminate_backend($1)", pid)
            with pytest.raises(TransientDependencyError) as caught:
                await asyncio.wait_for(task, 1)
            assert caught.value.code == "service_unavailable"
            await _assert_clean(pool)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_http_read_disconnect_cancels_actual_query_and_drains_pool():
    async with _runtime() as (admin, pool, database, execute):
        marker = "read_disconnect_probe"
        app = FastAPI()

        @app.get("/api/v1/tables/fixture/items/rows")
        async def read():
            return await execute(f"SELECT pg_sleep(10) /* {marker} */")

        app.add_middleware(NoStorePublicSurfacesMiddleware)
        app.add_middleware(CancelDisconnectedReadMiddleware)
        messages = asyncio.Queue()
        await messages.put({"type": "http.request", "body": b"", "more_body": False})
        sent = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/api/v1/tables/fixture/items/rows",
                 "headers": [], "query_string": b"", "scheme": "http", "http_version": "1.1",
                 "server": ("localhost", 80), "client": ("localhost", 1)}
        task = asyncio.create_task(app(scope, messages.get, send))
        try:
            await _wait_active(admin, database, marker)
            started = time.monotonic()
            await messages.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 1)
            assert time.monotonic() - started < 1
            assert await _active(admin, database, marker) is None
            assert sent == []
            await _assert_clean(pool)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_http_write_disconnect_does_not_replay_or_pretend_to_abort():
    async with _runtime() as (admin, pool, database, execute):
        marker = "write_completion_probe"
        calls = []
        app = FastAPI()

        @app.post("/api/v1/tables/fixture/sql")
        async def write():
            calls.append(1)
            return await execute("WITH changed AS (UPDATE fixture SET value=value+1 WHERE id=1 RETURNING value) "
                                 f"SELECT value, pg_sleep(0.2) FROM changed /* {marker} */")

        app.add_middleware(CancelDisconnectedReadMiddleware)
        messages = asyncio.Queue()
        await messages.put({"type": "http.request", "body": b"", "more_body": False})

        async def send(message):
            pass  # A disconnected client cannot rely on the returned response.

        scope = {"type": "http", "method": "POST", "path": "/api/v1/tables/fixture/sql",
                 "headers": [], "query_string": b"", "scheme": "http", "http_version": "1.1",
                 "server": ("localhost", 80), "client": ("localhost", 1)}
        task = asyncio.create_task(app(scope, messages.get, send))
        try:
            await _wait_active(admin, database, marker)
            await messages.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 1)
            assert calls == [1]
            await _assert_clean(pool, value=1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
