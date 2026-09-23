"""Partial driver results follow the ordinary ACL, hydration and response path."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import search_degradation_stats as sds
from app.services import search_service as ss
from app.services.vector_store import VectorSearchDegraded, VectorStoreUnavailable
from tests.test_search_archive_scope_vault_path_unit import VAULT_ID, _Conn, _hit, _install

pytestmark = pytest.mark.asyncio
# Any partial driver result. No shipped driver raises this today: the VChord
# shape completes a short page exactly instead of refusing it (akb#673).
_REASON = "partial_driver_result"


def _partial_store(monkeypatch, hits):
    search = AsyncMock(side_effect=VectorSearchDegraded(hits=hits, reason=_REASON))
    store = SimpleNamespace(vault_filter_supported=True, hybrid_search=search)
    monkeypatch.setattr(ss, "get_vector_store", lambda: store)
    monkeypatch.setattr(ss.sparse_encoder, "encode_query", AsyncMock(return_value=([7], [1.0])))
    return search


async def test_partial_exception_is_public_and_retains_filtered_driver_hits(monkeypatch):
    hits = [_hit(uuid.uuid4())]
    driver = _partial_store(monkeypatch, hits)
    result, reason = await ss.SearchService()._run_vector_search(
        query_text="known term", query_embedding=[0.1], candidate_source_ids=None,
        candidate_vault_ids=[str(VAULT_ID)], source_types=["document"], limit=2,
    )
    assert issubclass(VectorSearchDegraded, VectorStoreUnavailable)
    assert result is hits
    assert reason == _REASON
    assert driver.call_args.kwargs["vault_ids"] == [str(VAULT_ID)]
    assert driver.call_args.kwargs["source_types"] == ["document"]


@pytest.mark.parametrize("empty", [False, True])
async def test_partial_search_response_preserves_hydration_and_accounting(monkeypatch, empty):
    active, archived = uuid.uuid4(), uuid.uuid4()
    _install(monkeypatch, _Conn({active: "draft", archived: "archived"}))
    # Actual hydration must filter authoritative archived metadata rather than
    # returning exception hits directly.
    _partial_store(monkeypatch, [] if empty else [_hit(archived), _hit(active)])
    sds.reset()
    try:
        response = await ss.SearchService().search(
            "known term", vault="mine", user_id=str(uuid.UUID(int=1)), limit=2,
        )
        assert response.degraded is True
        assert response.degradation_reason == _REASON
        assert response.returned == (0 if empty else 1)
        assert len(response.results) == (0 if empty else 1)
        if not empty:
            assert response.results[0].title == f"Doc {active.int}"
            assert response.results[0].vault == "mine"
            assert response.excluded == {"archived": 1}
        delta = next(iter(sds._pending.values()))
        assert delta.observed == delta.degraded == 1
        assert delta.degraded_with_results == (0 if empty else 1)
        assert dict(delta.by_cause) == {_REASON: 1}
    finally:
        sds.reset()


async def test_denied_vault_never_calls_partial_driver(monkeypatch):
    _install(monkeypatch, _Conn({}))
    driver = _partial_store(monkeypatch, [_hit(uuid.uuid4())])
    service = ss.SearchService()
    monkeypatch.setattr(service, "_accessible_vault_ids", AsyncMock(return_value=[]))
    response = await service.search(
        "known term", vault="denied", user_id=str(uuid.UUID(int=1)), limit=2,
    )
    assert response.results == []
    driver.assert_not_awaited()
