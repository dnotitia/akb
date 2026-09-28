"""Queries must use the numbering they were encoded under, through execution."""
import asyncio
import uuid
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.db import postgres
from app.services import search_service, sparse_encoder
from app.services.bm25_maintenance import BM25_VOCAB_EPOCH_LOCK_KEY
from scripts import compact_bm25_term_ids as compact
from tests.test_bm25_term_id_compaction_postgres import _installation

pytestmark = pytest.mark.asyncio


async def _seed(install, shape):
    async with install.pool.acquire() as c:
        await c.executemany("INSERT INTO bm25_vocab(term,term_id) VALUES($1,$2)",
                            [("alpha", 1), ("beta", 2), ("gamma", 100)])
        await c.execute("SELECT setval('bm25_term_id_seq',1000)")
        for number, term, term_id in [(1, "alpha", 1), (2, "beta", 2)]:
            ident = uuid.UUID(int=number)
            await c.execute(
                "INSERT INTO vector_index.chunks "
                "(chunk_id,source_type,source_id,vault_id,section_path,content,chunk_index) "
                "VALUES($1,'document',$1,$2,'',$3,0)", ident, uuid.UUID(int=9), term,
            )
            if shape == "vchord":
                await c.execute("UPDATE vector_index.chunks SET sparse_bm25=$2::bm25_catalog.bm25vector "
                                "WHERE chunk_id=$1", ident, f"{{{term_id}:1}}")
            else:
                await c.execute("INSERT INTO vector_index.posting VALUES($1,$2,1)", term_id, ident)
        if shape == "vchord":
            await c.execute("REINDEX INDEX vector_index.idx_vi_chunks_bm25")


async def _search(install, encoded, *, epoch=True):
    kw = {"query_sparse_epoch": encoded.epoch} if epoch else {}
    return await install.store.hybrid_search(
        query_text="alpha", query_dense=None, query_sparse_indices=encoded.indices,
        query_sparse_values=encoded.values, source_ids=None, limit=10,
        prefetch_per_leg=50, **kw,
    )


@pytest.mark.parametrize("shape", ["vchord", "posting"])
async def test_stale_query_is_refused_instead_of_returning_another_terms_document(monkeypatch, shape):
    async with _installation(monkeypatch, shape=shape) as install:
        await _seed(install, shape)
        encoded = await sparse_encoder.encode_query_at_epoch("alpha", sparse_shape=shape)
        assert [h.chunk_id for h in await _search(install, encoded)] == [str(uuid.UUID(int=1))]
        async with install.pool.acquire() as c:
            await compact.apply(c, schema="vector_index", shape=shape)
        with pytest.raises(sparse_encoder.VocabularyEpochMoved):
            await _search(install, encoded)
        with pytest.raises(sparse_encoder.VocabularyEpochMoved):
            await _search(install, encoded, epoch=False)
        refreshed = await sparse_encoder.encode_query_at_epoch("alpha", sparse_shape=shape)
        assert [h.chunk_id for h in await _search(install, refreshed)] == [str(uuid.UUID(int=1))]


@pytest.mark.parametrize("shape", ["vchord", "posting"])
async def test_service_reencodes_after_compaction_between_encoding_and_execution(monkeypatch, shape):
    async with _installation(monkeypatch, shape=shape) as install:
        await _seed(install, shape)
        search = install.store.hybrid_search
        attempts = []

        async def compact_then_search(**kw):
            attempts.append(kw["query_sparse_epoch"])
            if len(attempts) == 1:
                async with install.pool.acquire() as c:
                    await compact.apply(c, schema="vector_index", shape=shape)
            return await search(**kw)

        monkeypatch.setattr(install.store, "hybrid_search", compact_then_search)
        monkeypatch.setattr(search_service, "get_vector_store", lambda: install.store)
        hits, reason = await search_service.SearchService()._run_vector_search(
            query_text="alpha", query_embedding=None, candidate_source_ids=None, limit=10,
        )
        assert reason is None
        assert [h.chunk_id for h in hits] == [str(uuid.UUID(int=1))]
        assert attempts == [0, 1]


@pytest.mark.parametrize("shape", ["vchord", "posting"])
async def test_held_fence_degrades_service_and_refuses_direct_search(monkeypatch, shape):
    async with _installation(monkeypatch, shape=shape) as install:
        await _seed(install, shape)
        encoded = await sparse_encoder.encode_query_at_epoch("alpha", sparse_shape=shape)
        holder = await asyncpg.connect(install.dsn)
        try:
            await holder.execute("SELECT pg_advisory_lock($1)", BM25_VOCAB_EPOCH_LOCK_KEY)
            with pytest.raises(sparse_encoder.VocabularyRenumberingInProgress):
                await asyncio.wait_for(_search(install, encoded), 2)
            monkeypatch.setattr(search_service, "get_vector_store", lambda: install.store)
            hits, reason = await asyncio.wait_for(search_service.SearchService()._run_vector_search(
                query_text="alpha", query_embedding=None, candidate_source_ids=None, limit=10,
            ), 2)
            assert hits == [] and reason == "bm25_renumbering"
        finally:
            await holder.close()


async def test_sparse_search_needs_no_nested_pool_acquire(monkeypatch):
    async with _installation(monkeypatch) as install:
        await _seed(install, "vchord")
        encoded = await sparse_encoder.encode_query_at_epoch("alpha", sparse_shape="vchord")
        tiny_pool = await asyncpg.create_pool(install.dsn, min_size=1, max_size=1)
        previous_pool = postgres._pool
        postgres._pool = tiny_pool
        try:
            hits = await asyncio.wait_for(_search(install, encoded), 2)
            assert [h.chunk_id for h in hits] == [str(uuid.UUID(int=1))]
        finally:
            postgres._pool = previous_pool
            await tiny_pool.close()


async def test_refused_sparse_leg_drains_its_concurrent_dense_leg(monkeypatch):
    async with _installation(monkeypatch) as install:
        await _seed(install, "vchord")
        encoded = await sparse_encoder.encode_query_at_epoch("alpha", sparse_shape="vchord")
        cancelled = asyncio.Event()
        dense_task = None

        async def waiting_dense(*_args, **_kw):
            nonlocal dense_task
            dense_task = asyncio.current_task()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        monkeypatch.setattr(install.store, "_search_dense", waiting_dense)
        monkeypatch.setattr(install.store, "_ensure_codec", AsyncMock())
        holder = await asyncpg.connect(install.dsn)
        try:
            await holder.execute("SELECT pg_advisory_lock($1)", BM25_VOCAB_EPOCH_LOCK_KEY)
            with pytest.raises(sparse_encoder.VocabularyRenumberingInProgress):
                await install.store.hybrid_search(
                    query_text="alpha", query_dense=[1, 0, 0, 0],
                    query_sparse_indices=encoded.indices, query_sparse_values=encoded.values,
                    query_sparse_epoch=encoded.epoch, source_ids=None, limit=10, prefetch_per_leg=50,
                )
            assert cancelled.is_set(), "a refused sparse leg left its dense query holding a connection"
        finally:
            if dense_task is not None and not dense_task.done():
                dense_task.cancel()
                await asyncio.gather(dense_task, return_exceptions=True)
            await holder.close()


async def test_sparse_execution_holds_the_fence_until_its_query_finishes(monkeypatch):
    async with _installation(monkeypatch) as install:
        await _seed(install, "vchord")
        encoded = await sparse_encoder.encode_query_at_epoch("alpha", sparse_shape="vchord")
        entered, finish = asyncio.Event(), asyncio.Event()
        search_sparse = install.store._search_sparse

        async def paused_sparse(*args, **kw):
            entered.set()
            await finish.wait()
            return await search_sparse(*args, **kw)

        monkeypatch.setattr(install.store, "_search_sparse", paused_sparse)
        search = asyncio.create_task(_search(install, encoded))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with install.pool.acquire() as c:
                with pytest.raises(compact.Refused, match="held the fence"):
                    await compact.apply(c, schema="vector_index", shape="vchord", lock_timeout=0.05)
        finally:
            finish.set()
            hits = await asyncio.wait_for(search, 2)
        assert [h.chunk_id for h in hits] == [str(uuid.UUID(int=1))]
        async with install.pool.acquire() as c:
            assert (await compact.apply(c, schema="vector_index", shape="vchord")).changed
