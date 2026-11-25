"""Real hybrid indexing with a deterministic embedding provider."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable

import asyncpg
import httpx
import pytest

from app.config import settings
from app.services import embed_worker, index_service, sparse_encoder
from app.services.vector_store import factory


@pytest.fixture
async def drain_search_index(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Callable[[], Awaitable[None]]]:
    monkeypatch.setattr(settings, "vector_store_driver", "pgvector")
    monkeypatch.setattr(settings, "vector_store_dsn", "")
    monkeypatch.setattr(settings, "vector_store_sparse_shape", "auto")
    monkeypatch.setattr(settings, "_sparse_shape_decision", None)
    monkeypatch.setattr(settings, "embed_base_url", "http://embed.test/v1")
    monkeypatch.setattr(settings, "embed_dimensions", 3)
    monkeypatch.setattr(settings, "rerank_enabled", False)
    monkeypatch.setattr(factory, "_singleton", None)
    monkeypatch.setattr(sparse_encoder, "_stats_cache", None)

    def embed(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        body = json.loads(request.content)
        vector = [1.0] + [0.0] * (body["dimensions"] - 1)
        return httpx.Response(200, json={
            "data": [
                {"index": index, "embedding": vector}
                for index, _ in enumerate(body["input"])
            ],
        })

    async def drain() -> None:
        while await embed_worker._process_once():
            pass
        pending = await pg_pool.fetch(
            "SELECT id, vector_last_error FROM chunks WHERE vector_indexed_at IS NULL"
        )
        assert not pending, pending

    async with httpx.AsyncClient(transport=httpx.MockTransport(embed)) as embedding_api:
        monkeypatch.setattr(index_service.http_pool, "get_client", lambda: embedding_api)
        await factory.decide_sparse_shape_for_settings()
        await factory.get_vector_store().ensure_collection()
        yield drain
