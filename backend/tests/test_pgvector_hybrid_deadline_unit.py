"""Real task scheduling around the driver; only PostgreSQL I/O is replaced."""
from contextlib import asynccontextmanager
import asyncio
import time
import uuid
from types import SimpleNamespace

import asyncpg
from asyncpg.transaction import Transaction
import pytest

from app.services.vector_store import VectorSearchDegraded
from app.services.vector_store.pgvector import PgvectorStore

pytestmark = pytest.mark.asyncio
HIT_ID = str(uuid.UUID(int=1))


class _Connection:
    def __init__(self, pool):
        self.pool = pool
        self.released = False
        self.terminated = False
        self.release_delay = 0.0
        self.statement_timeouts = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, *args):
        if "statement_timeout" in sql:
            self.statement_timeouts.append((sql, args))

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_xact_lock_shared" in sql:
            return True
        if "FROM bm25_vocab_epoch" in sql:
            return 0
        raise AssertionError(f"Unexpected test connection query: {sql}")

    def terminate(self):
        self.terminated = True
        self.pool.return_connection(self)


class _Acquire:
    def __init__(self, pool, timeout):
        self.pool, self.timeout, self.connection = pool, timeout, None

    async def acquire(self):
        async with asyncio.timeout(self.timeout):
            await self.pool.slots.acquire()
        self.connection = _Connection(self.pool)
        self.pool.connections.append(self.connection)
        return self.connection

    def __await__(self):
        return self.acquire().__await__()

    async def __aenter__(self):
        return await self.acquire()

    async def __aexit__(self, *args):
        await self.pool.release(self.connection)


class _Pool:
    def __init__(self, slots=2):
        self.slots = asyncio.Semaphore(slots)
        self.connections = []

    def acquire(self, *, timeout=None):
        return _Acquire(self, timeout)

    def return_connection(self, conn):
        if not conn.released:
            conn.released = True
            self.slots.release()

    async def release(self, conn, *, timeout=None):
        try:
            async with asyncio.timeout(timeout):
                await asyncio.sleep(conn.release_delay)
        except (TimeoutError, asyncio.CancelledError):
            conn.terminate()
            raise
        self.return_connection(conn)


def _store(monkeypatch, *, pool=None, budget=0.15):
    pool = pool or _Pool()
    store = PgvectorStore(dsn=None, schema="vector_index", dense_dim=2, sparse_shape="vchord",
                          retrieval_timeout_secs=budget)

    async def noop(*args):
        pass

    async def get_pool():
        return pool

    async def fast(*args, **kwargs):
        return [HIT_ID]

    async def payloads(conn, ids):
        return [dict(chunk_id=cid, source_type="document", source_id=cid,
                     section_path="", content="retained peer") for cid in ids]

    monkeypatch.setattr(store, "ensure_collection", noop)
    monkeypatch.setattr(store, "_ensure_codec", noop)
    monkeypatch.setattr(store, "_pool", get_pool)
    monkeypatch.setattr(store, "_search_dense", fast)
    monkeypatch.setattr(store, "_search_sparse", fast)
    monkeypatch.setattr(store, "_fetch_payloads", payloads)
    return store, pool


async def _search(store):
    # A watchdog fails old unbounded behavior without hanging the test runner.
    async with asyncio.timeout(0.8):
        return await store.hybrid_search(
            query_text="term", query_dense=[1.0, 0.0], query_sparse_indices=[7],
            query_sparse_values=[1.0], source_ids=None, vault_ids=[str(uuid.UUID(int=2))],
            source_types=["document"], limit=5, prefetch_per_leg=10,
        )


@pytest.mark.parametrize("slow_leg", ["dense", "sparse"])
async def test_completed_peer_survives_leg_deadline(monkeypatch, slow_leg):
    store, pool = _store(monkeypatch)
    stopped = asyncio.Event()

    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(store, f"_search_{slow_leg}", slow)
    with pytest.raises(VectorSearchDegraded) as caught:
        await _search(store)
    assert caught.value.reason == f"{slow_leg}_leg_timeout"
    assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
    assert stopped.is_set()
    assert all(conn.released for conn in pool.connections)


async def test_both_slow_legs_return_empty_degradation(monkeypatch):
    store, pool = _store(monkeypatch)

    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "_search_dense", slow)
    monkeypatch.setattr(store, "_search_sparse", slow)
    with pytest.raises(VectorSearchDegraded) as caught:
        await _search(store)
    assert caught.value.reason == "retrieval_timeout"
    assert caught.value.hits == []
    assert all(conn.released for conn in pool.connections)


@pytest.mark.parametrize("failure_first", [True, False])
async def test_connection_failure_keeps_peer_that_finishes_before_or_after_it(monkeypatch, failure_first):
    store, pool = _store(monkeypatch)
    first_done = asyncio.Event()

    async def failed(*args, **kwargs):
        if not failure_first:
            await first_done.wait()
        first_done.set()
        raise asyncpg.ConnectionDoesNotExistError("connection lost")

    async def fast(*args, **kwargs):
        if failure_first:
            await first_done.wait()
        first_done.set()
        return [HIT_ID]

    monkeypatch.setattr(store, "_search_dense", fast)
    monkeypatch.setattr(store, "_search_sparse", failed)
    with pytest.raises(VectorSearchDegraded) as caught:
        await _search(store)
    assert caught.value.reason == "sparse_leg_failed"
    assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
    assert all(conn.released for conn in pool.connections)


@pytest.mark.parametrize("error", [ValueError("bad vector"), asyncpg.InsufficientPrivilegeError("denied"),
                                   asyncpg.UndefinedTableError("bad SQL")])
async def test_programming_and_authorization_errors_propagate_after_peer_cleanup(monkeypatch, error):
    store, pool = _store(monkeypatch)
    peer_started, peer_stopped = asyncio.Event(), asyncio.Event()

    async def failed(*args, **kwargs):
        await peer_started.wait()
        raise error

    async def slow(*args, **kwargs):
        peer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            peer_stopped.set()

    monkeypatch.setattr(store, "_search_dense", failed)
    monkeypatch.setattr(store, "_search_sparse", slow)
    with pytest.raises(type(error)) as caught:
        await _search(store)
    assert caught.value is error
    assert peer_stopped.is_set()
    assert all(conn.released for conn in pool.connections)


async def test_caller_cancellation_waits_for_both_connections_to_release(monkeypatch):
    store, pool = _store(monkeypatch)
    both_started = asyncio.Event()
    running = 0

    async def slow(conn, **kwargs):
        nonlocal running
        conn.release_delay = 0.02
        running += 1
        if running == 2:
            both_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "_search_dense", slow)
    monkeypatch.setattr(store, "_search_sparse", slow)
    task = asyncio.create_task(_search(store))
    await both_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(pool.connections) == 2
    assert all(conn.released for conn in pool.connections)


async def test_pool_wait_consumes_leg_deadline_before_any_query(monkeypatch):
    store, pool = _store(monkeypatch, pool=_Pool(slots=0))
    started = time.monotonic()
    with pytest.raises(VectorSearchDegraded) as caught:
        await _search(store)
    assert caught.value.reason == "retrieval_timeout"
    assert caught.value.hits == []
    assert time.monotonic() - started < 0.4
    assert pool.connections == []


async def test_stalled_release_is_terminated_and_does_not_erase_peer(monkeypatch):
    store, pool = _store(monkeypatch)

    async def slow(conn, **kwargs):
        conn.release_delay = 10
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "_search_sparse", slow)
    started = time.monotonic()
    with pytest.raises(VectorSearchDegraded) as caught:
        await _search(store)
    assert caught.value.reason == "sparse_leg_timeout"
    assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
    assert time.monotonic() - started < 0.4
    assert any(conn.terminated for conn in pool.connections)
    assert all(conn.released for conn in pool.connections)


async def test_payload_uses_remaining_request_budget(monkeypatch):
    store, pool = _store(monkeypatch)

    async def payloads(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "_fetch_payloads", payloads)
    started = time.monotonic()
    with pytest.raises(VectorSearchDegraded) as caught:
        await _search(store)
    assert caught.value.reason == "retrieval_timeout"
    assert caught.value.hits == []
    assert time.monotonic() - started < 0.4
    assert all(conn.released for conn in pool.connections)


@pytest.mark.parametrize("failure", [asyncpg.ConfigurationLimitExceededError, asyncpg.OutOfMemoryError])
async def test_resource_exhaustion_preserves_the_healthy_peer(monkeypatch, failure):
    store, _ = _store(monkeypatch)

    async def refused(*args, **kwargs):
        raise failure("synthetic resource limit")

    monkeypatch.setattr(store, "_search_sparse", refused)
    with pytest.raises(VectorSearchDegraded) as caught:
        await _search(store)
    assert caught.value.reason == "sparse_leg_failed"
    assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]


class _ShieldedReleasePool(_Pool):
    """asyncpg shields its internal release task from its caller's cancellation."""
    def __init__(self):
        super().__init__()
        self.release_started = asyncio.Event()
        self.release_tasks = []

    async def release(self, conn, *, timeout=None):
        task = asyncio.create_task(super().release(conn, timeout=timeout))
        self.release_tasks.append(task)
        self.release_started.set()
        await asyncio.shield(task)


async def test_cancellation_during_release_awaits_the_shielded_pool_reset(monkeypatch):
    pool = _ShieldedReleasePool()
    store, _ = _store(monkeypatch, pool=pool, budget=0.5)

    async def fast(conn, **kwargs):
        conn.release_delay = 0.03
        return [HIT_ID]

    monkeypatch.setattr(store, "_search_dense", fast)
    monkeypatch.setattr(store, "_search_sparse", fast)
    task = asyncio.create_task(_search(store))
    await pool.release_started.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(conn.released for conn in pool.connections)
        assert all(reset.done() for reset in pool.release_tasks)
    finally:
        await asyncio.gather(*pool.release_tasks, return_exceptions=True)


async def test_cancellation_during_timeout_cleanup_still_drains_connections(monkeypatch):
    pool = _ShieldedReleasePool()
    store, _ = _store(monkeypatch, pool=pool, budget=0.5)

    async def slow(conn, **kwargs):
        conn.release_delay = 0.03
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "_search_dense", slow)
    monkeypatch.setattr(store, "_search_sparse", slow)
    task = asyncio.create_task(_search(store))
    await pool.release_started.wait()  # Deadline has already cancelled the legs.
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(conn.released for conn in pool.connections)
        assert all(reset.done() for reset in pool.release_tasks)
    finally:
        await asyncio.gather(*pool.release_tasks, return_exceptions=True)


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_stalled_transaction_rollback_is_terminated_with_bounded_caller_cleanup(monkeypatch, repeat_cancel):
    store, pool = _store(monkeypatch, budget=30)
    query_started, rollback_stopped = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def transaction(conn):
        try:
            yield conn
        except asyncio.CancelledError:
            # Model a lost network path during ROLLBACK: a second cancellation
            # after connection termination must release the coroutine too.
            try:
                await asyncio.Event().wait()
            finally:
                rollback_stopped.set()
            raise

    async def slow(conn, **kwargs):
        query_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(_Connection, "transaction", transaction)
    monkeypatch.setattr(store, "_search_dense", slow)
    monkeypatch.setattr(store, "_search_sparse", slow)
    task = asyncio.create_task(_search(store))
    await query_started.wait()
    started = time.monotonic()
    task.cancel()
    if repeat_cancel:
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 0.4
    assert rollback_stopped.is_set()
    assert any(conn.terminated for conn in pool.connections)
    assert all(conn.released for conn in pool.connections)
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("pgvector-search-")]


async def test_reset_timeout_does_not_mask_authorization_error(monkeypatch):
    store, pool = _store(monkeypatch)

    async def denied(conn, **kwargs):
        conn.release_delay = 10
        raise asyncpg.InsufficientPrivilegeError("denied")

    monkeypatch.setattr(store, "_search_sparse", denied)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _search(store)
    assert all(conn.released for conn in pool.connections)


async def test_programming_error_during_deadline_cleanup_is_not_relabelled_timeout(monkeypatch):
    store, pool = _store(monkeypatch)

    async def broken_cleanup(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            raise ValueError("query cleanup bug")

    monkeypatch.setattr(store, "_search_sparse", broken_cleanup)
    with pytest.raises(ValueError, match="query cleanup bug"):
        await _search(store)
    assert all(conn.released for conn in pool.connections)


@pytest.mark.parametrize("original_error", [None, asyncpg.InsufficientPrivilegeError("denied")])
async def test_supervisor_closed_transaction_preserves_timeout_or_original_error(monkeypatch, original_error):
    store, pool = _store(monkeypatch)

    @asynccontextmanager
    async def transaction(conn):
        try:
            yield conn
        except BaseException:
            while not conn.terminated:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
            raise asyncpg.InterfaceError(
                "cannot call Transaction.__aexit__(): the underlying connection is closed"
            ) from None

    async def failed(conn, **kwargs):
        if original_error is not None:
            raise original_error
        await asyncio.Event().wait()

    monkeypatch.setattr(_Connection, "transaction", transaction)
    monkeypatch.setattr(store, "_search_sparse", failed)
    if original_error is not None:
        with pytest.raises(type(original_error)) as caught:
            await _search(store)
        assert caught.value is original_error
    else:
        with pytest.raises(VectorSearchDegraded) as caught:
            await _search(store)
        assert caught.value.reason == "sparse_leg_timeout"
        assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
    assert all(conn.released for conn in pool.connections)


async def test_unmarked_closed_transaction_error_propagates(monkeypatch):
    store, _ = _store(monkeypatch)

    async def broken(*args, **kwargs):
        raise asyncpg.InterfaceError(
            "cannot call Transaction.__aexit__(): the underlying connection is closed"
        )

    monkeypatch.setattr(store, "_search_sparse", broken)
    with pytest.raises(asyncpg.InterfaceError):
        await _search(store)


async def test_successful_reset_crossing_deadline_preserves_authorization_error(monkeypatch):
    store, pool = _store(monkeypatch, budget=0.5)
    denied = asyncpg.InsufficientPrivilegeError("denied")

    async def failed(conn, **kwargs):
        await asyncio.sleep(0.335)
        conn.release_delay = 0.04
        raise denied

    monkeypatch.setattr(store, "_search_sparse", failed)
    with pytest.raises(asyncpg.InsufficientPrivilegeError) as caught:
        await _search(store)
    assert caught.value is denied
    assert all(conn.released for conn in pool.connections)


@pytest.fixture
def real_transactions(monkeypatch):
    def transaction(conn):
        # Exercise asyncpg's actual exception chaining during transaction and
        # savepoint cleanup. Only the connection I/O below is substituted.
        if not hasattr(conn, "_pool_release_ctr"):
            conn._pool_release_ctr = 0
            conn._top_xact = None
            conn._protocol = SimpleNamespace(is_in_transaction=lambda: False)
            conn.is_closed = lambda: conn.terminated
            conn._get_unique_id = lambda prefix: prefix + "_1"
        return Transaction(conn, None, False, False)

    monkeypatch.setattr(_Connection, "transaction", transaction)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("caller_cancel", [False, True])
@pytest.mark.parametrize("error", [asyncpg.InsufficientPrivilegeError("denied"),
                                   asyncpg.UndefinedTableError("bad SQL"), ValueError("bad vector")])
async def test_cancelled_rollback_preserves_query_error_unless_caller_cancels(
    monkeypatch, real_transactions, nested, caller_cancel, error,
):
    store, pool = _store(monkeypatch)
    rollback_started = asyncio.Event()
    original_execute = _Connection.execute

    async def execute(conn, sql, *args):
        if sql.startswith("ROLLBACK"):
            rollback_started.set()
            await asyncio.Event().wait()
        return await original_execute(conn, sql, *args)

    async def failed(conn, **kwargs):
        if nested:
            async with conn.transaction():
                raise error
        raise error

    monkeypatch.setattr(_Connection, "execute", execute)
    monkeypatch.setattr(store, "_search_sparse", failed)
    task = asyncio.create_task(_search(store))
    if caller_cancel:
        await asyncio.wait_for(rollback_started.wait(), timeout=0.5)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if caller_cancel else type(error)) as caught:
        await task
    if not caller_cancel:
        assert caught.value is error
    assert all(conn.released for conn in pool.connections)


async def test_cancelled_savepoint_commit_does_not_resurrect_handled_error(monkeypatch, real_transactions):
    store, pool = _store(monkeypatch)
    original_execute = _Connection.execute

    async def execute(conn, sql, *args):
        if sql.startswith("RELEASE SAVEPOINT"):
            await asyncio.Event().wait()
        return await original_execute(conn, sql, *args)

    async def recovered(conn, **kwargs):
        try:
            raise ValueError("already handled")
        except ValueError:
            async with conn.transaction():
                return [HIT_ID]

    monkeypatch.setattr(_Connection, "execute", execute)
    monkeypatch.setattr(store, "_search_sparse", recovered)
    with pytest.raises(VectorSearchDegraded) as caught:
        await _search(store)
    assert caught.value.reason == "sparse_leg_timeout"
    assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
    assert all(conn.released for conn in pool.connections)
