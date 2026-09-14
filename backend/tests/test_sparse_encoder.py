import asyncio

import pytest

from app.services.sparse_encoder import (
    _BM25_RECOMPUTE_DELTA_THRESHOLD,
    _english_token_variants,
    _state_requires_recompute,
)


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


def _stats_row(**overrides):
    from app.services import sparse_encoder

    row = {
        "source_revision": 10_000,
        "source_chunk_count": 960_000,
        "tokenizer_name": "kiwi",
        "tokenizer_version": sparse_encoder._kiwi_version,
    }
    row.update(overrides)
    return row


def test_refresh_gate_ignores_token_bearing_doc_count_mismatch():
    # The old gate compared 960k raw chunks with ~954k token-bearing docs and
    # rebuilt forever. total_docs is deliberately absent from the new decision.
    assert not _state_requires_recompute(_stats_row(), 10_000)


def test_refresh_gate_detects_same_count_content_replacements():
    assert _state_requires_recompute(
        _stats_row(),
        10_000 + _BM25_RECOMPUTE_DELTA_THRESHOLD,
    )


def test_refresh_gate_updates_small_corpus_without_waiting_for_fifty_changes():
    assert _state_requires_recompute(
        _stats_row(source_chunk_count=12),
        10_001,
    )


def test_refresh_gate_uses_conditional_count_for_set_based_truncate():
    row = _stats_row()
    assert not _state_requires_recompute(row, 10_001)
    assert _state_requires_recompute(row, 10_001, live_chunk_count=0)


def test_refresh_gate_fails_safe_on_tokenizer_change_or_sequence_restore():
    assert _state_requires_recompute(
        _stats_row(tokenizer_version="old"),
        10_000,
    )
    assert _state_requires_recompute(_stats_row(), 9_999)


@pytest.mark.asyncio
async def test_stats_refresher_start_is_idempotent_and_keeps_one_health_runner(
    monkeypatch,
):
    from app.services import sparse_encoder
    from app.services._backfill import runner_snapshots

    await sparse_encoder.stop_stats_refresher()

    async def no_work() -> int:
        return 0

    monkeypatch.setattr(sparse_encoder._refresher, "_process_once", no_work)
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
