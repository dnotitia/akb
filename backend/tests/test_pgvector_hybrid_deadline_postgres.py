"""Exercise cancellation and pool reuse with asyncpg's real protocol and reset.

Uses AKB_VCHORD_TEST_DSN like the existing driver PostgreSQL suites. Each case
creates a disposable database with the vocabulary fence required by retrieval.
Only SQL I/O at the search-leg boundary is replaced; cancellation/reset is real.
"""
from contextlib import asynccontextmanager
import asyncio
import os
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.services.vector_store import VectorSearchDegraded
from app.services.vector_store.pgvector import PgvectorStore

pytestmark = pytest.mark.asyncio
HIT_ID = str(uuid.UUID(int=1))


@asynccontextmanager
async def _store(monkeypatch, *, statement_timeout=None):
    dsn = os.environ.get("AKB_VCHORD_TEST_DSN")
    if not dsn:
        pytest.skip("AKB_VCHORD_TEST_DSN is required for asyncpg cancellation coverage")
    # The root CI database has no application tables. Keep each protocol test
    # independent of suite ordering and of any pre-existing installation.
    name = f"akb_search_deadline_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        test_dsn = urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))
        pool = await asyncpg.create_pool(
            test_dsn, min_size=2, max_size=2, command_timeout=30,
            server_settings={"statement_timeout": statement_timeout} if statement_timeout else None,
        )
        async with pool.acquire() as conn:
            await conn.execute("CREATE TABLE bm25_vocab_epoch (id integer PRIMARY KEY, epoch bigint NOT NULL)")
            await conn.execute("INSERT INTO bm25_vocab_epoch VALUES (1, 0)")
        store = PgvectorStore(dsn=None, schema="vector_index", dense_dim=2, sparse_shape="vchord",
                              retrieval_timeout_secs=0.4)

        async def noop(*args):
            pass

        async def get_pool():
            return pool

        async def fast(conn, **kwargs):
            assert kwargs["filter_uuids"] == [uuid.UUID(int=2)]
            assert kwargs["source_type_values"] == ["document"]
            return [await conn.fetchval("SELECT $1::text", HIT_ID)]

        async def payloads(conn, ids):
            return await conn.fetch(
                "SELECT id AS chunk_id, 'document' AS source_type, id AS source_id, "
                "'' AS section_path, 'retained peer' AS content FROM unnest($1::text[]) AS id", ids,
            )

        monkeypatch.setattr(store, "ensure_collection", noop)
        monkeypatch.setattr(store, "_ensure_codec", noop)
        monkeypatch.setattr(store, "_pool", get_pool)
        monkeypatch.setattr(store, "_search_dense", fast)
        monkeypatch.setattr(store, "_search_sparse", fast)
        monkeypatch.setattr(store, "_fetch_payloads", payloads)
        yield store, pool
    finally:
        try:
            if pool is not None:
                await asyncio.wait_for(pool.close(), timeout=2)
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.close()


async def _search(store):
    return await asyncio.wait_for(store.hybrid_search(
        query_text="term", query_dense=[1.0, 0.0], query_sparse_indices=[7],
        query_sparse_values=[1.0], source_ids=None, vault_ids=[str(uuid.UUID(int=2))],
        source_types=["document"], limit=5, prefetch_per_leg=10,
    ), timeout=2)


async def _assert_pool_reusable(pool, original_timeout):
    # Holding both simultaneously checks both holders, including the cancelled
    # one. Query success also proves no open/aborted transaction or active query.
    async with pool.acquire(timeout=0.3) as first, pool.acquire(timeout=0.3) as second:
        for conn in (first, second):
            assert await conn.fetchval("SELECT 1") == 1
            assert await conn.fetchval("SHOW statement_timeout") == original_timeout


@pytest.mark.parametrize("slow_leg", ["dense", "sparse"])
async def test_pg_sleep_timeout_preserves_peer_and_resets_both_connections(monkeypatch, slow_leg):
    async with _store(monkeypatch) as (store, pool):
        async with pool.acquire() as conn:
            original_timeout = await conn.fetchval("SHOW statement_timeout")

        async def slow(conn, **kwargs):
            await conn.execute("SELECT pg_sleep(10)")
            return [HIT_ID]

        monkeypatch.setattr(store, f"_search_{slow_leg}", slow)
        started = time.monotonic()
        with pytest.raises(VectorSearchDegraded) as caught:
            await _search(store)
        assert caught.value.reason == f"{slow_leg}_leg_timeout"
        assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
        assert time.monotonic() - started < 0.7
        await _assert_pool_reusable(pool, original_timeout)


@pytest.mark.parametrize("failed_leg", ["dense", "sparse"])
async def test_connection_loss_during_timeout_setup_preserves_peer(monkeypatch, failed_leg):
    async with _store(monkeypatch) as (store, pool):
        async with pool.acquire() as conn:
            original_timeout = await conn.fetchval("SHOW statement_timeout")

        peer_finished = asyncio.Event()
        peer_leg = "sparse" if failed_leg == "dense" else "dense"
        search_peer = getattr(store, f"_search_{peer_leg}")
        execute = asyncpg.Connection.execute

        async def finish_peer(conn, **kwargs):
            ids = await search_peer(conn, **kwargs)
            peer_finished.set()
            return ids

        async def lose_connection_during_setup(conn, sql, *args, **kwargs):
            if ("statement_timeout" in sql
                    and asyncio.current_task().get_name() == f"pgvector-search-{failed_leg}"):
                await peer_finished.wait()
                # Drop the real server connection during setup. asyncpg's
                # transaction exit will try to unwind an already closed socket.
                return await execute(conn, "SELECT pg_terminate_backend(pg_backend_pid())")
            return await execute(conn, sql, *args, **kwargs)

        monkeypatch.setattr(store, f"_search_{peer_leg}", finish_peer)
        monkeypatch.setattr(asyncpg.Connection, "execute", lose_connection_during_setup)
        with pytest.raises(VectorSearchDegraded) as caught:
            await _search(store)
        assert caught.value.reason == f"{failed_leg}_leg_failed"
        assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
        await _assert_pool_reusable(pool, original_timeout)
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("pgvector-search-")]


async def test_exhausted_pool_stops_before_starting_queries(monkeypatch):
    async with _store(monkeypatch) as (store, pool):
        async with pool.acquire() as first, pool.acquire() as second:
            started = time.monotonic()
            with pytest.raises(VectorSearchDegraded) as caught:
                await _search(store)
            assert caught.value.reason == "retrieval_timeout"
            assert caught.value.hits == []
            assert time.monotonic() - started < 0.7
            assert await first.fetchval("SELECT 1") == await second.fetchval("SELECT 1") == 1


async def test_caller_cancellation_drains_active_postgres_queries(monkeypatch):
    async with _store(monkeypatch) as (store, pool):
        async with pool.acquire() as conn:
            original_timeout = await conn.fetchval("SHOW statement_timeout")
        both_started = asyncio.Event()
        started = 0

        async def slow(conn, **kwargs):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await conn.execute("SELECT pg_sleep(10)")
            return []

        monkeypatch.setattr(store, "_search_dense", slow)
        monkeypatch.setattr(store, "_search_sparse", slow)
        task = asyncio.create_task(_search(store))
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await _assert_pool_reusable(pool, original_timeout)
            assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("pgvector-search-")]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _StatsReadConnection:
    """Inject a real server error into just the best-effort statistics read."""
    def __init__(self, conn, *, timeout=False):
        self.conn, self.timeout = conn, timeout

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def fetchrow(self, sql, *args):
        if "pg_stats" in sql:
            return await self.conn.fetchrow("SELECT pg_sleep(10)" if self.timeout else "SELECT 1 / 0")
        return await self.conn.fetchrow(sql, *args)


async def test_failed_best_effort_statistics_read_does_not_abort_hybrid_transaction(monkeypatch):
    async with _store(monkeypatch) as (store, pool):
        sparse_id = str(uuid.UUID(int=3))
        async with pool.acquire() as conn:
            original_timeout = await conn.fetchval("SHOW statement_timeout")

        async def sparse(conn, **kwargs):
            selective = await store._filter_is_selective(
                _StatsReadConnection(conn), kwargs["filter_col"], kwargs["filter_uuids"],
            )
            assert selective is False
            # This is the actual fallback query's transaction state, after a
            # server-side division-by-zero, rather than a Python-only exception.
            assert await conn.fetchval("SELECT 1") == 1
            return [sparse_id]

        monkeypatch.setattr(store, "_search_sparse", sparse)
        hits = await _search(store)
        assert {hit.chunk_id for hit in hits} == {HIT_ID, sparse_id}
        await _assert_pool_reusable(pool, original_timeout)


async def test_statistics_statement_timeout_preserves_dense_peer_and_timeout_reason(monkeypatch):
    async with _store(monkeypatch) as (store, pool):
        async with pool.acquire() as conn:
            original_timeout = await conn.fetchval("SHOW statement_timeout")

        async def sparse(conn, **kwargs):
            await store._filter_is_selective(
                _StatsReadConnection(conn, timeout=True), kwargs["filter_col"], kwargs["filter_uuids"],
            )
            raise AssertionError("statistics timeout was swallowed and search continued")

        monkeypatch.setattr(store, "_search_sparse", sparse)
        with pytest.raises(VectorSearchDegraded) as caught:
            await _search(store)
        assert caught.value.reason == "sparse_leg_timeout"
        assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
        await _assert_pool_reusable(pool, original_timeout)


async def test_retrieval_deadline_preserves_stricter_session_statement_timeout(monkeypatch):
    async with _store(monkeypatch, statement_timeout="10ms") as (store, pool):
        sparse_id = str(uuid.UUID(int=3))

        async def sparse(conn, **kwargs):
            await conn.execute("SELECT pg_sleep(0.04)")
            return [sparse_id]

        monkeypatch.setattr(store, "_search_sparse", sparse)
        # 40ms is well inside the retrieval deadline, but the operator's 10ms
        # server limit must still cancel this leg and retain its completed peer.
        with pytest.raises(VectorSearchDegraded) as caught:
            await _search(store)
        assert caught.value.reason == "sparse_leg_timeout"
        assert [hit.chunk_id for hit in caught.value.hits] == [HIT_ID]
        await _assert_pool_reusable(pool, "10ms")
