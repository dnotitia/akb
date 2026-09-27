"""Bounded retries and explicit degradation when query numbering keeps moving."""
from unittest.mock import AsyncMock

import pytest

from app.services import search_service, sparse_encoder


@pytest.mark.asyncio
async def test_continuously_moving_query_epoch_has_a_bounded_degraded_result(monkeypatch):
    class Store:
        query_epoch_supported = True
        sparse_shape = "posting"
        hybrid_search = AsyncMock(side_effect=sparse_encoder.VocabularyEpochMoved(1, 2))

    store = Store()
    encode = AsyncMock(return_value=sparse_encoder.EncodedQuery([1], [1.0], 1))
    monkeypatch.setattr(search_service, "get_vector_store", lambda: store)
    monkeypatch.setattr(sparse_encoder, "encode_query_at_epoch", encode)
    hits, reason = await search_service.SearchService()._run_vector_search(
        query_text="alpha", query_embedding=None, candidate_source_ids=None, limit=10,
    )
    assert hits == [] and reason == "bm25_vocabulary_moved"
    assert store.hybrid_search.await_count == encode.await_count == 3
