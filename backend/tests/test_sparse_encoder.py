import asyncio

import pytest

from app.services.sparse_encoder import _english_token_variants


def test_english_token_variants_keep_original_and_match_past_tense():
    assert "graduate" in _english_token_variants("graduated")
    assert "graduat" in _english_token_variants("graduate")


def test_english_token_variants_match_regular_past_tense():
    assert "repaint" in _english_token_variants("repainted")


def test_english_token_variants_match_simple_plural():
    assert "wall" in _english_token_variants("walls")


def test_english_token_variants_drop_common_stopwords():
    assert _english_token_variants("what") == []
    assert _english_token_variants("did") == []
    assert _english_token_variants("my") == []


def test_english_token_variants_leave_non_ascii_untouched():
    assert _english_token_variants("쿠버네티스") == ["쿠버네티스"]


def test_english_token_variants_stem_plural_once_no_es_s_overlap():
    # "es" and bare "s" rules must be mutually exclusive — no "churche" noise.
    churches = _english_token_variants("churches")
    assert "church" in churches
    assert "churche" not in churches
    # "-ies" preempts the "-es"/"-s" rules — no "studi"/"studie" noise.
    studies = _english_token_variants("studies")
    assert "study" in studies
    assert "studi" not in studies
    assert "studie" not in studies


@pytest.mark.asyncio
async def test_stats_refresher_start_is_idempotent_and_keeps_one_health_runner(
    monkeypatch,
):
    from app.services import sparse_encoder
    from app.services._backfill import runner_snapshots

    await sparse_encoder.stop_stats_refresher()

    async def no_work() -> int:
        return 0

    async def bootstrap() -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(sparse_encoder._refresher, "_process_once", no_work)
    monkeypatch.setattr(sparse_encoder, "_bootstrap_recompute", bootstrap)
    before = sum(
        item["name"] == "bm25_stats_refresher" for item in runner_snapshots()
    )

    sparse_encoder.start_stats_refresher(3600)
    sparse_encoder.start_stats_refresher(3600)
    await asyncio.sleep(0)

    after = sum(
        item["name"] == "bm25_stats_refresher" for item in runner_snapshots()
    )
    assert before == after == 1
    assert sparse_encoder._refresher.is_running()

    await sparse_encoder.stop_stats_refresher()
