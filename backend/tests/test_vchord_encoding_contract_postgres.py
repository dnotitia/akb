"""Real Kiwi and PostgreSQL vocab identity across live and backfill encoding.

Uses only the disposable database created by the shared extension fixture.
"""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from app.services import sparse_encoder
from scripts.backfill_bm25_vector import _encode
from tests.test_bm25_vector_backfill_postgres import _corpus

pytestmark = pytest.mark.asyncio


async def test_kiwi_ids_survive_reencoding_backfill_and_missing_external_stats(monkeypatch):
    real_tokenize = sparse_encoder.tokenize
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_driver", "pgvector")
    # The migration backfill must explicitly choose VChord while serving posting.
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_sparse_shape", "posting")
    async with _corpus(monkeypatch, shape="vchord") as (_, pool):
        monkeypatch.setattr(sparse_encoder, "tokenize", real_tokenize)
        text = "검색 검색 데이터 데이터 engine engine engines"
        counts = Counter(await real_tokenize(text))
        assert "검색" in counts and "engine" in counts
        assert counts["검색"] == 2
        assert counts["engine"] >= 2
        async with pool.acquire() as conn:
            # Make accidental reads fail even if process-global stats are cached.
            await conn.execute("DROP TABLE bm25_stats")
            await conn.execute("ALTER TABLE bm25_vocab DROP COLUMN df")
        monkeypatch.setattr(sparse_encoder, "_stats_cache", None)
        first = dict(zip(*(await sparse_encoder.encode_document(text, sparse_shape="vchord"))))
        ids = await sparse_encoder.lookup_term_ids(counts)
        assert first == {ids[term]: float(count) for term, count in counts.items()}
        assert all(value > 0 and value.is_integer() for value in first.values())

        # Register unrelated vocabulary between repeated writes.
        await sparse_encoder.encode_document("새로운문서 unrelatedword", sparse_shape="vchord")
        second = dict(zip(*(await sparse_encoder.encode_document(text, sparse_shape="vchord"))))
        assert second == first
        assert await sparse_encoder.lookup_term_ids(counts) == ids
        literal = await _encode(text, asyncio.Semaphore(1))
        async with pool.acquire() as conn:
            # Compare the extension's parsed representation, not a duplicate
            # serialization implementation in this test.
            from app.services.vector_store.pgvector import _bm25vector_literal

            expected = _bm25vector_literal(list(first), list(first.values()))
            assert await conn.fetchval(
                "SELECT $1::bm25_catalog.bm25vector::text = $2::bm25_catalog.bm25vector::text",
                literal, expected,
            )

        query_text = "검색 검색 engine engine zzneverregisteredzz"
        query = dict(zip(*(await sparse_encoder.encode_query(query_text, sparse_shape="vchord"))))
        known_query_terms = set(await real_tokenize(query_text)) & set(ids)
        assert {"검색", "engine"} <= known_query_terms
        assert query == {ids[term]: 1.0 for term in known_query_terms}
        assert await sparse_encoder.lookup_term_ids(["zzneverregisteredzz"]) == {}
        assert await sparse_encoder.encode_document("", sparse_shape="vchord") == ([], [])
        assert await sparse_encoder.encode_query("", sparse_shape="vchord") == ([], [])
