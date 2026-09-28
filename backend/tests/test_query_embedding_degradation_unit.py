"""Configured embedding failures differ from intentional sparse-only mode."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.config import settings
from app.services import search_service as ss
from tests.test_search_archive_scope_vault_path_unit import _Conn, _hit, _install

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("embedding", [[], [[]], None, "exception"])
async def test_configured_embedding_failure_retains_sparse_results_and_marks_degraded(monkeypatch, embedding):
    doc = uuid.uuid4()
    _install(monkeypatch, _Conn({doc: "draft"}))
    monkeypatch.setattr(settings, "embed_base_url", "https://embedding.example/v1")
    generate = AsyncMock(side_effect=TimeoutError() if embedding == "exception" else None,
                         return_value=embedding)
    monkeypatch.setattr(ss, "generate_embeddings", generate)
    search = AsyncMock(return_value=[_hit(doc)])
    monkeypatch.setattr(ss, "get_vector_store", lambda: SimpleNamespace(vault_filter_supported=True, hybrid_search=search))
    monkeypatch.setattr(ss.sparse_encoder, "encode_query", AsyncMock(return_value=([1], [1.0])))
    response = await ss.SearchService().search("alpha", vault="mine", user_id=str(uuid.UUID(int=1)))
    assert response.returned == 1
    assert response.degraded
    assert response.degradation_reason == "query_embedding_failed"
    assert search.call_args.kwargs["query_dense"] is None


async def test_unconfigured_embedding_is_healthy_sparse_only(monkeypatch):
    doc = uuid.uuid4()
    _install(monkeypatch, _Conn({doc: "draft"}))
    monkeypatch.setattr(settings, "embed_base_url", "")
    monkeypatch.setattr(ss, "generate_embeddings", AsyncMock(return_value=[[]]))
    search = AsyncMock(return_value=[_hit(doc)])
    monkeypatch.setattr(ss, "get_vector_store", lambda: SimpleNamespace(vault_filter_supported=True, hybrid_search=search))
    monkeypatch.setattr(ss.sparse_encoder, "encode_query", AsyncMock(return_value=([1], [1.0])))
    response = await ss.SearchService().search("alpha", vault="mine", user_id=str(uuid.UUID(int=1)))
    assert response.returned == 1
    assert not response.degraded
    assert search.call_args.kwargs["query_dense"] is None
