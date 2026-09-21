from __future__ import annotations

import pytest

from app.services import bm25_maintenance, sparse_encoder


def test_shared_lock_namespace_is_stable_and_signed_bigint_sized():
    assert bm25_maintenance.BM25_RECOMPUTE_LOCK_KEY == 987654321
    assert bm25_maintenance.BM25_BULK_OWNER_LOCK_KEY == bm25_maintenance.advisory_lock_key(
        "akb:bm25-maintenance:bulk:owner"
    )
    assert bm25_maintenance.BM25_BULK_CONTROL_LOCK_KEY == bm25_maintenance.advisory_lock_key(
        "akb:bm25-maintenance:bulk:control"
    )
    assert bm25_maintenance.BM25_BULK_OWNER_LOCK_KEY != bm25_maintenance.BM25_BULK_CONTROL_LOCK_KEY
    assert all(
        -(1 << 63) <= key < (1 << 63)
        for key in (
            bm25_maintenance.BM25_BULK_OWNER_LOCK_KEY,
            bm25_maintenance.BM25_BULK_CONTROL_LOCK_KEY,
        )
    )


@pytest.mark.asyncio
async def test_vector_queue_observation_is_local_and_read_only():
    class Conn:
        def __init__(self):
            self.query = None

        async def fetchval(self, query):
            self.query = query
            return True

    conn = Conn()
    assert await bm25_maintenance.vector_upsert_queue_nonempty(conn)
    assert "FROM chunks" in conn.query
    assert "vector_indexed_at IS NULL" in conn.query
    assert "vector_abandoned_at IS NULL" in conn.query


@pytest.mark.asyncio
async def test_active_recompute_observation_uses_only_granted_current_db_lock():
    class Conn:
        def __init__(self):
            self.query = None
            self.key = None

        async def fetchval(self, query, key):
            self.query = query
            self.key = key
            return True

    conn = Conn()
    assert await bm25_maintenance.active_bm25_recompute(conn)
    assert conn.key == bm25_maintenance.BM25_RECOMPUTE_LOCK_KEY
    assert "pg_locks" in conn.query
    assert "ExclusiveLock" in conn.query
    assert "granted" in conn.query
    assert "current_database()" in conn.query
    assert "pg_stat_activity" not in conn.query
    assert "bm25_recompute_run" not in conn.query


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *_exc):
                return None

        return Acquire()


class _RecomputeConn:
    def __init__(self, *, got_lock: bool, queue_pending: bool):
        self.got_lock = got_lock
        self.queue_pending = queue_pending
        self.queries: list[str] = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "pg_try_advisory_lock" in query:
            return self.got_lock
        if "vector_indexed_at IS NULL" in query:
            return self.queue_pending
        if "pg_advisory_unlock" in query:
            return True
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, *args):
        self.queries.append(query)
        assert "pg_advisory_unlock" in query
        return "SELECT 1"


@pytest.mark.asyncio
async def test_recompute_defers_before_opening_run_when_vector_queue_is_nonempty(
    monkeypatch,
):
    conn = _RecomputeConn(got_lock=True, queue_pending=True)
    monkeypatch.setattr(sparse_encoder, "get_pool", _async_return(_Pool(conn)))
    monkeypatch.setattr(sparse_encoder, "tokenizer_info", lambda: ("kiwi", "test"))
    monkeypatch.setattr(
        sparse_encoder,
        "_open_run",
        _unexpected_call("_open_run must not run while queue work is pending"),
    )

    result = await sparse_encoder.recompute_stats(defer_if_vector_queue=True)

    assert result["skipped"] is True
    assert (
        result["skip_reason"]
        == bm25_maintenance.BM25_RECOMPUTE_SKIP_REASON_VECTOR_QUEUE
    )
    assert result["skip_reason"] in bm25_maintenance.BM25_RECOMPUTE_SKIP_REASONS
    assert any("pg_try_advisory_lock" in query for query in conn.queries)
    assert any("FROM chunks" in query for query in conn.queries)
    assert any("pg_advisory_unlock" in query for query in conn.queries)


@pytest.mark.asyncio
async def test_manual_recompute_does_not_defer_for_vector_queue(monkeypatch):
    conn = _RecomputeConn(got_lock=True, queue_pending=True)
    monkeypatch.setattr(sparse_encoder, "get_pool", _async_return(_Pool(conn)))
    monkeypatch.setattr(sparse_encoder, "tokenizer_info", lambda: ("kiwi", "test"))
    monkeypatch.setattr(
        bm25_maintenance,
        "vector_upsert_queue_nonempty",
        _unexpected_call("manual recompute must not inspect the vector queue"),
    )
    monkeypatch.setattr(
        sparse_encoder,
        "_open_run",
        _unexpected_call("manual recompute reached its normal run path"),
    )

    with pytest.raises(AssertionError, match="manual recompute reached"):
        await sparse_encoder.recompute_stats()


@pytest.mark.asyncio
async def test_recompute_lock_skip_keeps_legacy_skipped_shape(monkeypatch):
    conn = _RecomputeConn(got_lock=False, queue_pending=True)
    monkeypatch.setattr(sparse_encoder, "get_pool", _async_return(_Pool(conn)))
    monkeypatch.setattr(sparse_encoder, "tokenizer_info", lambda: ("kiwi", "test"))

    result = await sparse_encoder.recompute_stats()

    assert result["skipped"] is True
    assert result["total_docs"] is None
    assert result["avgdl"] is None
    assert result["vocab_size"] is None
    assert result["skip_reason"] == bm25_maintenance.BM25_RECOMPUTE_SKIP_REASON_LOCK_HELD
    assert not any("FROM chunks" in query for query in conn.queries)


def _async_return(value):
    async def _return(*_args, **_kwargs):
        return value

    return _return


def _unexpected_call(message):
    async def _call(*_args, **_kwargs):
        raise AssertionError(message)

    return _call
