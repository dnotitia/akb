"""A failed retrieval leg must preserve the healthy scoped leg, on real PG."""
import asyncio
import uuid

import asyncpg
import pytest

from app.services import sparse_encoder
from app.services.vector_store import VectorSearchDegraded, VectorStoreUnavailable
from tests.test_bm25_query_epoch_postgres import _seed
from tests.test_bm25_term_id_compaction_postgres import _installation

pytestmark = pytest.mark.asyncio


async def _prepare(install):
    await _seed(install, "vchord")
    async with install.pool.acquire() as conn:
        await install.store._ensure_codec(conn)
        await conn.execute("UPDATE vector_index.chunks SET dense=$1", [1.0, 0.0, 0.0, 0.0])
    encoded = await sparse_encoder.encode_query_at_epoch("alpha", sparse_shape="vchord")
    return dict(
        query_text="alpha", query_dense=[1.0, 0.0, 0.0, 0.0],
        query_sparse_indices=encoded.indices, query_sparse_values=encoded.values,
        query_sparse_epoch=encoded.epoch, source_ids=None, limit=10, prefetch_per_leg=50,
    )


async def _timeout(conn, **_kwargs):
    # Real statement cancellation leaves a failed transaction on the sparse
    # leg. That connection must be released before payload hydration.
    async with conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = '20ms'")
        await conn.execute("SELECT pg_sleep(1)")
    return []


@pytest.mark.parametrize("failed", ["dense", "sparse"])
@pytest.mark.parametrize("scope", ["none", "vault", "source"])
async def test_statement_timeout_preserves_the_healthy_scoped_oracle(monkeypatch, failed, scope):
    async with _installation(monkeypatch) as install:
        kwargs = await _prepare(install)
        if scope == "vault":
            kwargs["vault_ids"] = [str(uuid.UUID(int=9))]
        elif scope == "source":
            kwargs["source_ids"] = [str(uuid.UUID(int=1))]
        oracle_kwargs = dict(kwargs)
        if failed == "dense":
            oracle_kwargs["query_dense"] = None
        else:
            oracle_kwargs["query_sparse_indices"] = []
            oracle_kwargs["query_sparse_values"] = []
        oracle = await install.store.hybrid_search(**oracle_kwargs)
        assert oracle
        monkeypatch.setattr(install.store, f"_search_{failed}", _timeout)
        with pytest.raises(VectorSearchDegraded) as exc:
            await asyncio.wait_for(install.store.hybrid_search(**kwargs), 3)
        assert exc.value.reason == f"{failed}_leg_failed"
        assert exc.value.hits == oracle
        async with install.pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1


@pytest.mark.parametrize("failure", [asyncpg.InsufficientPrivilegeError, asyncpg.UndefinedTableError])
async def test_acl_or_schema_failure_cancels_and_drains_the_other_leg(monkeypatch, failure):
    async with _installation(monkeypatch) as install:
        kwargs = await _prepare(install)
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def waiting(conn, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def refused(conn, **_kwargs):
            await entered.wait()
            raise failure("synthetic refused query")

        monkeypatch.setattr(install.store, "_search_dense", waiting)
        monkeypatch.setattr(install.store, "_search_sparse", refused)
        with pytest.raises(VectorStoreUnavailable) as exc:
            await asyncio.wait_for(install.store.hybrid_search(**kwargs), 3)
        assert not isinstance(exc.value, VectorSearchDegraded)
        assert cancelled.is_set()


async def test_both_legs_failing_produces_no_partial_results(monkeypatch):
    async with _installation(monkeypatch) as install:
        kwargs = await _prepare(install)
        monkeypatch.setattr(install.store, "_search_dense", _timeout)
        monkeypatch.setattr(install.store, "_search_sparse", _timeout)
        with pytest.raises(VectorStoreUnavailable) as exc:
            await asyncio.wait_for(install.store.hybrid_search(**kwargs), 3)
        assert not isinstance(exc.value, VectorSearchDegraded)


async def test_partial_search_releases_failed_connection_with_a_one_slot_pool(monkeypatch):
    async with _installation(monkeypatch) as install:
        kwargs = await _prepare(install)
        oracle = await install.store.hybrid_search(**{**kwargs, "query_dense": None})
        tiny = await asyncpg.create_pool(install.dsn, min_size=1, max_size=1)
        try:
            async def get_pool():
                return tiny
            monkeypatch.setattr(install.store, "_pool", get_pool)
            monkeypatch.setattr(install.store, "_search_dense", _timeout)
            with pytest.raises(VectorSearchDegraded) as exc:
                await asyncio.wait_for(install.store.hybrid_search(**kwargs), 3)
            assert exc.value.hits == oracle
        finally:
            await tiny.close()


async def test_lost_dense_backend_preserves_the_sparse_oracle(monkeypatch):
    async with _installation(monkeypatch) as install:
        kwargs = await _prepare(install)
        oracle = await install.store.hybrid_search(**{**kwargs, "query_dense": None})

        async def disconnected(conn, **_kwargs):
            pid = conn.get_server_pid()
            killer = await asyncpg.connect(install.dsn)
            async def terminate():
                await asyncio.sleep(0.05)
                await killer.execute("SELECT pg_terminate_backend($1)", pid)
            task = asyncio.create_task(terminate())
            try:
                await conn.fetchval("SELECT pg_sleep(5)")
            finally:
                await task
                await killer.close()
            return []

        monkeypatch.setattr(install.store, "_search_dense", disconnected)
        with pytest.raises(VectorSearchDegraded) as exc:
            await asyncio.wait_for(install.store.hybrid_search(**kwargs), 3)
        assert exc.value.reason == "dense_leg_failed"
        assert exc.value.hits == oracle


async def test_caller_cancellation_drains_both_legs(monkeypatch):
    async with _installation(monkeypatch) as install:
        kwargs = await _prepare(install)
        entered = [asyncio.Event(), asyncio.Event()]
        cancelled = [asyncio.Event(), asyncio.Event()]

        def waiting(number):
            async def query(conn, **_kwargs):
                entered[number].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled[number].set()
            return query

        monkeypatch.setattr(install.store, "_search_dense", waiting(0))
        monkeypatch.setattr(install.store, "_search_sparse", waiting(1))
        task = asyncio.create_task(install.store.hybrid_search(**kwargs))
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 3)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert all(event.is_set() for event in cancelled)
