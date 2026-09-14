"""Cheap CI coverage for timing output and no-access driver short circuits."""

import logging
import time
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
