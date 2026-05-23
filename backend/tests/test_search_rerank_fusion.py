from types import SimpleNamespace

import pytest

from app.exceptions import ValidationError
from app.models.document import SearchResponse
from app.services.search_service import (
    SearchService,
    fuse_original_and_reranked_hits,
    resolve_first_stage_unique_limit,
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
