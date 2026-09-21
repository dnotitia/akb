"""Run ownership and cancellation, including an actual database lock test."""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app.services.bm25_maintenance import (
    BM25_BULK_CONTROL_LOCK_KEY,
    BM25_BULK_OWNER_LOCK_KEY,
    BM25_RECOMPUTE_LOCK_KEY,
    active_bm25_recompute,
)
from scripts import bm25_run_lock
from scripts.bm25_run_lock import run_bulk_exclusive, run_exclusive

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


class GuardConnection:
    def __init__(self):
        self.closed = False
        self.listener = None
        self.held = []
        self.released = []

    async def fetchval(self, sql, key):
        if "pg_try_advisory_lock_shared" in sql:
            self.held.append(("shared", key))
            return True
        if "pg_try_advisory_lock" in sql:
            self.held.append(("exclusive", key))
            return True
        if "pg_advisory_unlock_shared" in sql:
            self.released.append(("shared", key))
            return True
        self.released.append(("exclusive", key))
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


class GuardPool:
    def __init__(self):
        self.connections = [GuardConnection(), GuardConnection()]
        self.depth = 0
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        conn = self.connections[self.depth]
        self.depth += 1
        try:
            yield conn
        finally:
            self.depth -= 1

    async def close(self):
        self.closed = True


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


async def test_bulk_guard_uses_two_main_db_sessions_and_both_lock_classes(monkeypatch):
    pool = GuardPool()
    create_kwargs = None

    async def create_pool(**kwargs):
        nonlocal create_kwargs
        create_kwargs = kwargs
        return pool

    async def operation():
        owner, control = pool.connections
        assert owner.held == [
            ("exclusive", BM25_BULK_OWNER_LOCK_KEY),
            ("shared", BM25_RECOMPUTE_LOCK_KEY),
        ]
        assert control.held == [
            ("exclusive", BM25_BULK_CONTROL_LOCK_KEY),
            ("shared", BM25_RECOMPUTE_LOCK_KEY),
        ]
        assert not owner.released and not control.released

    monkeypatch.setattr(bm25_run_lock.asyncpg, "create_pool", create_pool)
    await run_bulk_exclusive(operation)

    assert create_kwargs == {
        "dsn": bm25_run_lock.settings.asyncpg_dsn,
        "min_size": 2,
        "max_size": 2,
        "command_timeout": None,
    }
    assert pool.connections[0].released == [
        ("shared", BM25_RECOMPUTE_LOCK_KEY),
        ("exclusive", BM25_BULK_OWNER_LOCK_KEY),
    ]
    assert pool.connections[1].released == [
        ("shared", BM25_RECOMPUTE_LOCK_KEY),
        ("exclusive", BM25_BULK_CONTROL_LOCK_KEY),
    ]
    assert pool.closed


async def test_bulk_guard_loss_drains_before_surviving_session_releases(monkeypatch):
    pool = GuardPool()
    entered = asyncio.Event()
    drained = asyncio.Event()

    async def create_pool(**_kwargs):
        return pool

    async def operation():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            assert not pool.connections[0].released
            await asyncio.sleep(0)
            drained.set()

    monkeypatch.setattr(bm25_run_lock.asyncpg, "create_pool", create_pool)
    task = asyncio.create_task(run_bulk_exclusive(operation))
    await asyncio.wait_for(entered.wait(), 1)
    pool.connections[1].disconnect()
    with pytest.raises(RuntimeError, match="connection lost"):
        await task

    assert drained.is_set()
    assert pool.connections[0].released
    assert not pool.connections[1].released
    assert pool.closed


async def test_nested_guard_loss_cannot_interrupt_inner_operation_drain():
    loop = asyncio.get_running_loop()
    inner_lost = loop.create_future()
    outer_lost = loop.create_future()
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    outer_released = asyncio.Event()

    async def operation():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()

    async def inner_guard():
        await bm25_run_lock._run_until_guard_loss(
            inner_lost, operation, label="inner",
        )

    async def outer_guard():
        try:
            await bm25_run_lock._run_until_guard_loss(
                outer_lost, inner_guard, label="outer",
            )
        finally:
            outer_released.set()

    task = asyncio.create_task(outer_guard())
    await asyncio.wait_for(entered.wait(), 1)
    inner_lost.set_result(None)
    await asyncio.wait_for(cleanup_started.wait(), 1)

    # Losing the outer guard cancels the inner wrapper while it is awaiting
    # operation cleanup. The inner child must stay shielded and the outer guard
    # must remain held until that cleanup actually finishes.
    outer_lost.set_result(None)
    await asyncio.sleep(0)
    assert not task.done()
    assert not outer_released.is_set()
    assert not cleanup_finished.is_set()

    release_cleanup.set()
    with pytest.raises(RuntimeError, match="outer lock connection lost"):
        await asyncio.wait_for(task, 1)
    assert cleanup_finished.is_set()
    assert outer_released.is_set()


async def test_database_bulk_guards_exclude_recompute_and_peer_bulk_locks(monkeypatch):
    dsn = os.environ.get("AKB_VCHORD_TEST_DSN")
    if not dsn:
        pytest.skip("AKB_VCHORD_TEST_DSN required for main-DB bulk lock proof")

    monkeypatch.setattr(
        type(bm25_run_lock.settings),
        "asyncpg_dsn",
        property(lambda _settings: dsn),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    probe = await asyncpg.connect(dsn)

    async def hold():
        entered.set()
        await release.wait()

    task = asyncio.create_task(run_bulk_exclusive(hold))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not await probe.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)",
            BM25_RECOMPUTE_LOCK_KEY,
        )
        assert not await probe.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)",
            BM25_BULK_OWNER_LOCK_KEY,
        )
        assert not await probe.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)",
            BM25_BULK_CONTROL_LOCK_KEY,
        )

        release.set()
        await asyncio.wait_for(task, 3)

        for key in (
            BM25_RECOMPUTE_LOCK_KEY,
            BM25_BULK_OWNER_LOCK_KEY,
            BM25_BULK_CONTROL_LOCK_KEY,
        ):
            assert await probe.fetchval(
                "SELECT pg_try_advisory_lock($1::bigint)", key,
            )
            assert await probe.fetchval(
                "SELECT pg_advisory_unlock($1::bigint)", key,
            )
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await probe.execute("SELECT pg_advisory_unlock_all()")
        await probe.close()


async def test_active_recompute_observes_exclusive_legacy_lock_only():
    dsn = os.environ.get("AKB_VCHORD_TEST_DSN")
    if not dsn:
        pytest.skip("AKB_VCHORD_TEST_DSN required for recompute lock observation proof")

    observer = await asyncpg.connect(dsn)
    holder = await asyncpg.connect(dsn)
    try:
        assert not await active_bm25_recompute(observer)

        assert await holder.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)",
            BM25_RECOMPUTE_LOCK_KEY,
        )
        assert await active_bm25_recompute(observer)
        assert await holder.fetchval(
            "SELECT pg_advisory_unlock($1::bigint)",
            BM25_RECOMPUTE_LOCK_KEY,
        )
        assert not await active_bm25_recompute(observer)

        assert await holder.fetchval(
            "SELECT pg_try_advisory_lock_shared($1::bigint)",
            BM25_RECOMPUTE_LOCK_KEY,
        )
        assert not await active_bm25_recompute(observer)
        assert await holder.fetchval(
            "SELECT pg_advisory_unlock_shared($1::bigint)",
            BM25_RECOMPUTE_LOCK_KEY,
        )
        assert not await active_bm25_recompute(observer)
    finally:
        await holder.execute("SELECT pg_advisory_unlock_all()")
        await holder.close()
        await observer.close()


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
