"""Regression coverage for intentionally disabled query embeddings."""

from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.services import http_pool, search_service
from app.services.index_service import generate_embeddings
from app.services.search_service import SearchService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["first query", "second query"], [[], []]),
        ([], []),
    ],
)
async def test_disabled_embeddings_skip_http_and_return_sparse_only_markers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    texts: list[str],
    expected: list[list[float]],
) -> None:
    monkeypatch.setattr(settings, "embed_base_url", "")

    def unexpected_client():
        raise AssertionError("embedding HTTP client must not be acquired when disabled")

    monkeypatch.setattr(http_pool, "get_client", unexpected_client)

    with caplog.at_level(logging.WARNING, logger="akb.index"):
        assert await generate_embeddings(texts) == expected

    assert not caplog.records


@pytest.mark.asyncio
async def test_disabled_embedding_marker_keeps_search_sparse_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embed_base_url", "")

    def unexpected_client():
        raise AssertionError("embedding HTTP client must not be acquired when disabled")

    monkeypatch.setattr(http_pool, "get_client", unexpected_client)
    query_embedding = (await generate_embeddings(["lexical query"]))[0]
    captured: dict = {}

    async def encode_query(query: str) -> tuple[list[int], list[float]]:
        assert query == "lexical query"
        return [7], [0.5]

    class _Store:
        async def hybrid_search(self, **kwargs):
            captured.update(kwargs)
            return ["sparse-hit"]

    monkeypatch.setattr(search_service.sparse_encoder, "encode_query", encode_query)
    monkeypatch.setattr(search_service, "get_vector_store", lambda: _Store())

    hits, degradation_reason = await SearchService()._run_vector_search(
        query_text="lexical query",
        query_embedding=query_embedding,
        candidate_source_ids=["source-id"],
        limit=10,
    )

    assert hits == ["sparse-hit"]
    assert degradation_reason is None
    assert captured["query_dense"] == []
    assert captured["query_sparse_indices"] == [7]
    assert captured["query_sparse_values"] == [0.5]
