"""Exercise asyncpg's real holder while a search supervisor cancels cleanup."""

import asyncio

from asyncpg.pool import PoolConnectionHolder
import pytest

from app.services.vector_store import pgvector
from tests.test_pgvector_hybrid_deadline_unit import _Pool, _store


class _HolderPool(_Pool):
    """Replace only protocol I/O; retain asyncpg's holder reset/detach code."""

    def __init__(self):
        super().__init__()
        self._generation = 0
        self._reset = None
        self._queue = asyncio.Queue()
        self.release_started = asyncio.Event()
        self.resume_cancellation = asyncio.Event()
        self.release_tasks = []
        self.holders = []
        self.reset_count = 0

    async def release(self, conn, *, timeout=None):
        pool = self

        class Protocol:
            queries_count = 0

            def _is_cancelling(self):
                return True

            async def _wait_for_cancellation(self):
                pool.release_started.set()
                await pool.resume_cancellation.wait()

        holder = PoolConnectionHolder(
            self, max_queries=50_000, setup=None, max_inactive_time=0,
        )
        holder._con = conn
        holder._generation = self._generation
        holder._in_use = asyncio.get_running_loop().create_future()
        self.holders.append(holder)
        conn._protocol = Protocol()
        conn.is_closed = lambda: conn.terminated

        async def reset(*, timeout):
            self.reset_count += 1

        def terminate():
            conn.terminated = True
            # Connection.terminate() invokes this synchronously in asyncpg.
            # A supervisor doing it during release clears holder._con under
            # the cancellation waiter, which then crashes at _con.reset().
            holder._release_on_close()

        conn.reset = reset
        conn.terminate = terminate
        release = asyncio.create_task(holder.release(timeout))
        self.release_tasks.append(release)
        # Match Pool.release's shield around the real holder operation.
        await asyncio.shield(release)


@pytest.mark.asyncio
async def test_supervisor_does_not_terminate_connection_owned_by_asyncpg_release(monkeypatch):
    pool = _HolderPool()
    store, _ = _store(monkeypatch, pool=pool, budget=30)
    original_stop = pgvector._stop_search_tasks
    original_wait = asyncio.wait

    async def exhausted_cleanup(tasks, connections, *, cleanup_deadline, **kwargs):
        # Model a rollback consuming the cleanup allowance before release has
        # finished its cancellation handshake. No wall-clock race is needed.
        assert pool.release_started.is_set()
        await original_stop(
            tasks, connections, cleanup_deadline=asyncio.get_running_loop().time(),
            **kwargs,
        )

    async def resume_after_expired_wait(fs, *, timeout=None, **kwargs):
        result = await original_wait(fs, timeout=timeout, **kwargs)
        if timeout == 0 and result[1]:
            # Wake the holder, then return synchronously to the supervisor.
            # If it still owns this connection, terminate runs before the
            # holder can resume and dereference _con for reset.
            pool.resume_cancellation.set()
        return result

    monkeypatch.setattr(pgvector, "_stop_search_tasks", exhausted_cleanup)
    monkeypatch.setattr(asyncio, "wait", resume_after_expired_wait)
    search = asyncio.create_task(store.hybrid_search(
        query_text="term", query_dense=None, query_sparse_indices=[7],
        query_sparse_values=[1.0], source_ids=None, limit=5, prefetch_per_leg=10,
    ))
    try:
        await asyncio.wait_for(pool.release_started.wait(), timeout=1)
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(search, timeout=1)
        assert all(task.done() for task in pool.release_tasks)
        outcomes = await asyncio.gather(*pool.release_tasks, return_exceptions=True)
        assert outcomes == [None]
        assert not any(conn.terminated for conn in pool.connections)
        assert pool.reset_count == 1
        assert all(holder._in_use is None for holder in pool.holders)
        assert pool._queue.qsize() == 1
        assert not [
            task for task in asyncio.all_tasks()
            if task.get_name().startswith("pgvector-search-")
        ]
    finally:
        pool.resume_cancellation.set()
        search.cancel()
        await asyncio.gather(search, *pool.release_tasks, return_exceptions=True)
