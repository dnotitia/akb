from types import SimpleNamespace

import pytest

from app.exceptions import ValidationError
from app.models.document import SearchResponse
from app.services.search_service import (
    SearchService,
    clamp_search_limit,
    fuse_original_and_reranked_hits,
    resolve_first_stage_unique_limit,
    vault_path_eligible,
)

FUSION_K = 60


def _hits(*names: str):
    return [SimpleNamespace(name=name, score=0.0) for name in names]


def test_rerank_fusion_preserves_first_stage_vote():
    hits = _hits("a", "b", "c", "d", "e")

    fused = fuse_original_and_reranked_hits(
        hits,
        [(4, 0.9), (3, 0.8), (2, 0.7), (1, 0.6), (0, 0.5)],
        FUSION_K,
    )

    assert [h.name for h in fused[:5]] == ["a", "e", "b", "d", "c"]
    assert fused[0].score == pytest.approx(
        1 / (FUSION_K + 1) + 1 / (FUSION_K + 5)
    )


def test_rerank_fusion_ignores_bad_or_duplicate_indexes():
    hits = _hits("a", "b", "c")

    fused = fuse_original_and_reranked_hits(
        hits,
        [(-1, 1.0), (99, 1.0), (1, 0.9), (1, 0.1)],
        FUSION_K,
    )

    assert [h.name for h in fused] == ["b", "a", "c"]


def test_first_stage_unique_limit_uses_rerank_prefetch_when_rerank_is_on():
    assert resolve_first_stage_unique_limit(
        limit=5,
        rerank_enabled=True,
        rerank_prefetch=30,
        search_prefetch=0,
    ) == 30


def test_first_stage_unique_limit_can_prefetch_without_rerank():
    assert resolve_first_stage_unique_limit(
        limit=5,
        rerank_enabled=False,
        rerank_prefetch=30,
        search_prefetch=30,
    ) == 30


def test_first_stage_unique_limit_keeps_legacy_rerank_off_default():
    assert resolve_first_stage_unique_limit(
        limit=5,
        rerank_enabled=False,
        rerank_prefetch=30,
        search_prefetch=0,
    ) == 5


def test_search_response_defaults_truncated_false_no_hint():
    """A response built without explicit truncated/hint must default to
    'not truncated, no hint' — the most common case (small corpus or
    prefetch pool not filled). Mirrors how the empty-result paths in
    search_service.py:243 / :262 still construct SearchResponse."""
    r = SearchResponse(query="x", total=0, returned=0, total_matches=0, results=[])
    assert r.truncated is False
    assert r.hint is None


def test_search_response_carries_truncated_signal():
    r = SearchResponse(
        query="x",
        total=2,
        returned=2,
        total_matches=30,
        truncated=True,
        hint="Prefetch pool was capped...",
        results=[],
    )
    assert r.truncated is True
    assert r.hint and "Prefetch pool" in r.hint


def test_clamp_search_limit(monkeypatch):
    """Server-side limit clamp (issue #189): an over-large limit is capped to
    search_limit_max, and a zero/negative limit floors at 1, so a direct REST
    call or non-validating client can't blow up the vector-store prefetch."""
    from app.config import settings

    monkeypatch.setattr(settings, "search_limit_max", 50, raising=False)
    assert clamp_search_limit(1000) == 50  # over the ceiling → clamped
    assert clamp_search_limit(50) == 50  # at the ceiling → unchanged
    assert clamp_search_limit(10) == 10  # under → unchanged
    assert clamp_search_limit(0) == 1  # zero → floored to 1
    assert clamp_search_limit(-5) == 1  # negative → floored to 1
    # Honors a per-deployment override.
    monkeypatch.setattr(settings, "search_limit_max", 200, raising=False)
    assert clamp_search_limit(1000) == 200


def test_search_response_degraded_defaults_false():
    """A response built without explicit degraded fields must default to
    'not degraded' — a genuine zero-match, NOT a swallowed store failure."""
    r = SearchResponse(query="x", total=0, returned=0, total_matches=0, results=[])
    assert r.degraded is False
    assert r.degradation_reason is None


def test_vault_path_eligible(monkeypatch):
    """The VAULT path (issue #189 Phase 2) is taken ONLY with the flag on, the
    pgvector driver, and NO doc-level filter; otherwise the source_ids path
    (flag-off parity = byte-identical to before)."""
    from app.config import settings

    monkeypatch.setattr(settings, "vault_filter_enabled", True, raising=False)
    monkeypatch.setattr(settings, "vector_store_driver", "pgvector", raising=False)

    def call(**kw):
        base = {"collection": None, "doc_type": None, "tags": None, "source_uris": None}
        base.update(kw)
        return vault_path_eligible(**base)

    assert call() is True  # flag + pgvector + no filters → vault path
    assert call(collection="specs") is False  # any doc-level filter → source path
    assert call(doc_type="note") is False
    assert call(tags=["x"]) is False
    assert call(source_uris=["akb://v/doc/x"]) is False

    # flag off → never (parity with pre-Phase-2 behavior)
    monkeypatch.setattr(settings, "vault_filter_enabled", False, raising=False)
    assert call() is False

    # other driver → never (only pgvector stores vault_id)
    monkeypatch.setattr(settings, "vault_filter_enabled", True, raising=False)
    monkeypatch.setattr(settings, "vector_store_driver", "qdrant", raising=False)
    assert call() is False


@pytest.mark.asyncio
async def test_run_vector_search_forwards_vault_ids_to_driver(monkeypatch):
    """The VAULT path threads candidate_vault_ids into the driver's hybrid_search
    (and leaves source_ids None), so pgvector can filter by vault instead of an
    enumerated source-id list."""
    import app.services.search_service as ss

    svc = ss.SearchService()
    captured = {}

    async def encode_ok(_q):
        return [1], [1.0]

    class _Store:
        async def hybrid_search(self, **kw):
            captured.update(kw)
            return []

    monkeypatch.setattr(ss.sparse_encoder, "encode_query", encode_ok)
    monkeypatch.setattr(ss, "get_vector_store", lambda: _Store())

    await svc._run_vector_search(
        query_text="q", query_embedding=[0.1, 0.2],
        candidate_source_ids=None, candidate_vault_ids=["v1", "v2"], limit=10,
    )
    assert captured["vault_ids"] == ["v1", "v2"]
    assert captured["source_ids"] is None


@pytest.mark.asyncio
async def test_run_vector_search_surfaces_store_and_sparse_failures(monkeypatch):
    """_run_vector_search returns (hits, degradation_reason) and must classify
    failures instead of swallowing them into a silent [] (issue #189):
      - store raises VectorStoreUnavailable → 'vector_store_unavailable'
      - store raises any other Exception     → 'vector_store_error'
      - sparse encoder fails + no dense leg  → 'sparse_encoder_failed' (can't search)
      - sparse encoder fails + dense leg ok  → dense-only hits, 'sparse_encoder_degraded'
      - all healthy                          → hits, None
    """
    import app.services.search_service as ss

    svc = ss.SearchService()

    async def encode_ok(_q):
        return [1, 2], [0.5, 0.5]

    async def encode_fail(_q):
        raise RuntimeError("sparse encoder down")

    class _Store:
        def __init__(self, behavior):
            self.behavior = behavior

        async def hybrid_search(self, **_kw):
            if self.behavior == "unavailable":
                raise ss.VectorStoreUnavailable("store down")
            if self.behavior == "boom":
                raise RuntimeError("filter-size overflow")
            return [SimpleNamespace(source_type="document", source_id="d1", score=1.0)]

    def use_store(behavior):
        monkeypatch.setattr(ss, "get_vector_store", lambda: _Store(behavior))

    async def run(*, embedding):
        return await svc._run_vector_search(
            query_text="q", query_embedding=embedding,
            candidate_source_ids=["s1"], limit=10,
        )

    # store outage (transient) → vector_store_unavailable
    monkeypatch.setattr(ss.sparse_encoder, "encode_query", encode_ok)
    use_store("unavailable")
    hits, reason = await run(embedding=[0.1, 0.2])
    assert hits == [] and reason == "vector_store_unavailable"

    # unexpected store error (e.g. seahorse IN overflow) → vector_store_error
    use_store("boom")
    hits, reason = await run(embedding=[0.1, 0.2])
    assert hits == [] and reason == "vector_store_error"

    # all healthy → hits, no degradation
    use_store("ok")
    hits, reason = await run(embedding=[0.1, 0.2])
    assert len(hits) == 1 and reason is None

    # sparse encoder down + a dense leg → dense-only, flagged degraded
    monkeypatch.setattr(ss.sparse_encoder, "encode_query", encode_fail)
    use_store("ok")
    hits, reason = await run(embedding=[0.1, 0.2])
    assert len(hits) == 1 and reason == "sparse_encoder_degraded"

    # sparse encoder down + NO dense leg → can't search → degraded empty (not OOV)
    hits, reason = await run(embedding=None)
    assert hits == [] and reason == "sparse_encoder_failed"


def test_search_response_carries_degraded_signal():
    """When the vector store fails (outage / seahorse filter-size overflow),
    the empty result is flagged degraded with a cause — no longer a silent []
    (issue #189)."""
    r = SearchResponse(
        query="x",
        total=0,
        returned=0,
        total_matches=0,
        degraded=True,
        degradation_reason="vector_store_error",
        results=[],
    )
    assert r.degraded is True
    assert r.degradation_reason == "vector_store_error"


@pytest.mark.asyncio
async def test_search_requires_vault_or_user_id():
    """Mirror of the same guard in `grep`: a caller that forwards
    neither `vault` nor `user_id` would otherwise fall through to an
    unscoped cross-vault scan. All current callers (REST /search, MCP
    akb_search) forward `user_id`, so this is defense-in-depth for
    any future caller that forgets."""
    svc = SearchService()
    with pytest.raises(ValidationError):
        await svc.search(query="anything")
    with pytest.raises(ValidationError):
        await svc.search(query="anything", vault=None, user_id=None)
    # Either argument present is enough — no raise.
    # (Don't actually run the search; just verify the guard returns
    # control flow back so something further can happen. Catching
    # downstream errors keeps this a pure-guard unit test that
    # doesn't need a DB.)
    try:
        await svc.search(query="x", vault="v")
    except ValidationError:
        pytest.fail("Should not raise ValidationError when vault is set")
    except Exception:
        pass  # downstream (DB / embedding) is fine to fail; we only assert the guard
