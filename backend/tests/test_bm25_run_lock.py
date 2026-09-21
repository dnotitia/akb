"""Run ownership and cancellation, including an actual database lock test."""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest

from scripts.bm25_run_lock import run_exclusive

pytestmark = pytest.mark.asyncio


class Connection:
    def __init__(self, available=True):
        self.available = available
        self.closed = False
        self.listener = None
        self.unlocked = False

    async def fetchval(self, sql, key):
        if "pg_try_advisory_lock" in sql:
            return self.available
        self.unlocked = True
        return True

    def add_termination_listener(self, callback):
        self.listener = callback

    def remove_termination_listener(self, callback):
        assert self.listener == callback
        self.listener = None

    def is_closed(self):
        return self.closed

    def disconnect(self):
        self.closed = True
        self.listener(self)
        self.listener = None


class Pool:
    def __init__(self, conn):
        self.conn = conn
        self.control = Connection()
        self.depth = 0

    @asynccontextmanager
    async def acquire(self):
        conn = self.conn if self.depth == 0 else self.control
        self.depth += 1
        try:
            yield conn
        finally:
            self.depth -= 1


async def test_busy_lock_never_starts_operation():
    ran = False

    async def operation():
        nonlocal ran
        ran = True

    with pytest.raises(RuntimeError, match="another BM25"):
        await run_exclusive(Pool(Connection(False)), "s", operation)
    assert not ran


async def test_failure_releases_lock_after_operation_finishes():
    conn = Connection()

    async def operation():
        assert not conn.unlocked
        raise ValueError("operation failed")

    with pytest.raises(ValueError, match="operation failed"):
        await run_exclusive(Pool(conn), "s", operation)
    assert conn.unlocked
    assert conn.listener is None


@pytest.mark.parametrize("disconnect", [False, True])
async def test_loss_or_cancellation_drains_operation_before_releasing(disconnect):
    conn = Connection()
    entered = asyncio.Event()
    drained = asyncio.Event()

    async def operation():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            assert not conn.unlocked
            await asyncio.sleep(0)
            drained.set()

    task = asyncio.create_task(run_exclusive(Pool(conn), "s", operation))
    await asyncio.wait_for(entered.wait(), 1)
    if disconnect:
        conn.disconnect()
        with pytest.raises(RuntimeError, match="connection lost"):
            await task
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert drained.is_set()
    assert conn.unlocked is not disconnect
    assert conn.listener is None


async def test_database_lock_excludes_other_process_connections_and_releases():
    dsn = os.environ.get("AKB_VCHORD_TEST_DSN")
    if not dsn:
        pytest.skip("AKB_VCHORD_TEST_DSN required for independent-connection lock proof")
    schema = "lock_test_" + uuid.uuid4().hex
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    async def empty():
        pass

    task = asyncio.create_task(run_exclusive(pool, schema, hold))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(RuntimeError, match="another BM25"):
            await run_exclusive(pool, schema, empty)
        await run_exclusive(pool, schema + "_other", empty)
        release.set()
        await task
        await run_exclusive(pool, schema, empty)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close()


async def test_control_session_stop_holds_owner_guard_until_drain(capsys):
    import json
    dsn = os.environ.get("AKB_VCHORD_TEST_DSN")
    if not dsn:
        pytest.skip("AKB_VCHORD_TEST_DSN required for cancellation ownership proof")
    schema = "lock_stop_" + uuid.uuid4().hex
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()

    async def empty():
        pass

    task = asyncio.create_task(run_exclusive(pool, schema, operation, announce=True))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("BM25_LOCK "))
        receipt = json.loads(line[len("BM25_LOCK "):])
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT pg_terminate_backend($1)", receipt["pid"])
        await asyncio.wait_for(draining.wait(), 3)
        with pytest.raises(RuntimeError, match="another BM25"):
            await run_exclusive(pool, schema, empty)
        release.set()
        with pytest.raises(RuntimeError, match="connection lost"):
            await task
        await run_exclusive(pool, schema, empty)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close()
