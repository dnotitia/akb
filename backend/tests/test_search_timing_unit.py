"""Cheap CI coverage for timing output and no-access driver short circuits."""

import logging
import time
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.search_service import _log_search_timing
from app.services.vector_store.pgvector import PgvectorStore


@pytest.mark.parametrize("source_ids,vault_ids", [([], None), (None, []), ([], [])])
async def test_empty_scope_does_not_initialize_or_query_database(source_ids, vault_ids):
    store = PgvectorStore(dsn=None, schema="vector_index", dense_dim=4, sparse_shape="posting")
    store.ensure_collection = AsyncMock(side_effect=AssertionError("must not touch DB"))
    assert await store.hybrid_search(
        query_text="private query", query_dense=[1.0, 0.0, 0.0, 0.0],
        query_sparse_indices=[1], query_sparse_values=[1.0], source_ids=source_ids,
        vault_ids=vault_ids, limit=10, prefetch_per_leg=50,
    ) == []
    store.ensure_collection.assert_not_awaited()


def test_search_timing_reports_stage_milliseconds(caplog):
    with caplog.at_level(logging.INFO, logger="akb.search"):
        _log_search_timing(time.perf_counter(), {"embedding": 0.125, "retrieval": 0.25}, 3)
    assert "embedding_ms=125.00" in caplog.text
    assert "retrieval_ms=250.00" in caplog.text
    assert "rerank_ms=0.00" in caplog.text
    assert "returned=3" in caplog.text


@pytest.mark.parametrize(
    ("filter_col", "expects_materialized"),
    [("source_id", True), ("vault_id", False)],
)
async def test_sparse_posting_plan_materializes_only_source_candidates(
    filter_col, expects_materialized
):
    class _Connection:
        sql = ""
        fetch_kwargs = {}

        async def fetch(self, sql, *_args, **kwargs):
            self.sql = sql
            self.fetch_kwargs = kwargs
            return []

    store = PgvectorStore(
        dsn=None,
        schema="vector_index",
        dense_dim=4,
        sparse_shape="posting",
    )
    conn = _Connection()

    await store._search_sparse(
        conn,
        terms=[1, 2],
        weights=[1.0, 1.0],
        filter_uuids=[uuid.uuid4()],
        filter_col=filter_col,
        limit=20,
    )

    assert ("candidate_chunks AS MATERIALIZED" in conn.sql) is expects_materialized
    assert conn.fetch_kwargs == {"timeout": 30.0}


async def test_pgvector_search_budget_overrides_main_pool_statement_timeout():
    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _Connection:
        def __init__(self):
            self.executed = []
            self.fetch_kwargs = []

        def transaction(self):
            return _Transaction()

        async def execute(self, sql, *args):
            self.executed.append((sql, args))

        async def fetch(self, _sql, *_args, **kwargs):
            self.fetch_kwargs.append(kwargs)
            return []

    class _Acquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *_args):
            return False

    class _Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return _Acquire(self.conn)

    conn = _Connection()
    store = PgvectorStore(
        dsn=None,
        schema="vector_index",
        dense_dim=4,
        sparse_shape="posting",
        search_timeout_secs=60,
    )
    store.ensure_collection = AsyncMock()
    store._ensure_codec = AsyncMock()
    store._pool = AsyncMock(return_value=_Pool(conn))

    assert await store.hybrid_search(
        query_text="",
        query_dense=[1.0, 0.0, 0.0, 0.0],
        query_sparse_indices=[],
        query_sparse_values=[],
        source_ids=[str(uuid.uuid4())],
        limit=10,
        prefetch_per_leg=20,
    ) == []

    assert any(
        "set_config('statement_timeout'" in sql and args == ("60000ms",)
        for sql, args in conn.executed
    )
    assert conn.fetch_kwargs == [{"timeout": 60.0}]
