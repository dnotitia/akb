from types import SimpleNamespace

import pytest

from app.services.search_service import (
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
