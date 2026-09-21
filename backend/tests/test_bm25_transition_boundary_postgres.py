"""Real-PG cutover boundaries that NULL counts and moving marks cannot prove.

Reuses the isolated database fixture and real encoder/store from the existing
backfill suite. No production connection or fixtures are used.
"""
import asyncio
import uuid

import pytest

from app.services import sparse_encoder
from scripts import backfill_bm25_vector as bf
from tests.test_bm25_vector_backfill_postgres import _SCHEMA, _corpus, _write_old

pytestmark = pytest.mark.asyncio


async def _clock(pool):
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT clock_timestamp()")


async def _body_vector(pool, chunk_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            'SELECT content, indexed_at, sparse_bm25::text AS vector '
            'FROM vector_index.chunks WHERE chunk_id=$1', chunk_id,
        )


async def _expected(pool, content):
    literal = await bf._encode(content, asyncio.Semaphore(1))
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT $1::bm25_catalog.bm25vector::text', literal)


async def _posting_write(store, conn, chunk_id, content):
    terms, weights = await sparse_encoder.encode_document(content, sparse_shape="posting")
    await store.upsert_one(
        conn=conn, chunk_id=str(chunk_id), source_type="document",
        source_id=str(chunk_id), vault_id=str(uuid.UUID(int=1)),
        content=content, section_path="", chunk_index=0, dense=None,
        sparse_indices=terms, sparse_values=weights,
    )


async def test_identity_skip_is_repaired_after_the_writer_drains(monkeypatch):
    """Existing tests prove skip; this proves exact content repair afterward."""
    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=7)
        protected = await _clock(pool)
        await _write_old(store, pool, cid, "original text", 0)
        await bf._prepare(pool, _SCHEMA)
        await bf._pass(pool, _SCHEMA, None)
        old = await _body_vector(pool, cid)
        committed = asyncio.Event()
        real_apply = bf._apply

        async def race(pool_, schema, rows):
            if not committed.is_set():
                async with pool.acquire() as writer:
                    async with writer.transaction():
                        await _posting_write(store, writer, cid, "replacement entirely new")
                committed.set()
            return await real_apply(pool_, schema, rows)

        with monkeypatch.context() as patch:
            patch.setattr(bf, "_apply", race)
            assert await bf._pass(pool, _SCHEMA, protected) == 0
        assert committed.is_set()
        skipped = await _body_vector(pool, cid)
        assert skipped["content"] == "replacement entirely new"
        assert skipped["vector"] == old["vector"]
        assert await bf._pass(pool, _SCHEMA, protected) == 1
        fixed = await _body_vector(pool, cid)
        assert fixed["vector"] == await _expected(pool, fixed["content"])
        assert fixed["vector"] != old["vector"]


async def test_advancing_mark_misses_late_commit_but_protected_mark_catches(monkeypatch):
    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=3)
        await _write_old(store, pool, cid, "old content", 0)
        await bf._prepare(pool, _SCHEMA)
        await bf._pass(pool, _SCHEMA, None)
        old = await _body_vector(pool, cid)
        protected = await _clock(pool)
        async with pool.acquire() as writer:
            async with writer.transaction():
                started = await writer.fetchval("SELECT now()")
                advancing = await _clock(pool)
                assert protected < started < advancing
                # Cursor pass finishes while the older transaction is still
                # open; its later posting write uses transaction-start NOW().
                assert await bf._pass(pool, _SCHEMA, advancing) == 0
                await _posting_write(store, writer, cid, "late committed replacement")
            # The writer is committed/drained before either final sweep.
        late = await _body_vector(pool, cid)
        assert late["indexed_at"] == started
        assert late["vector"] == old["vector"]
        assert late["vector"] != await _expected(pool, late["content"])
        # Executable counterexample: NULL=0 and the advanced window owes 0,
        # while the non-NULL vector still represents the old text.
        assert await bf._counts(pool, _SCHEMA, advancing) == (0, 0)
        assert await bf._pass(pool, _SCHEMA, advancing) == 0
        assert await bf._pass(pool, _SCHEMA, protected) == 1
        fixed = await _body_vector(pool, cid)
        assert fixed["vector"] == await _expected(pool, fixed["content"])


async def test_valid_concurrent_index_does_not_close_posting_write_window(monkeypatch):
    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=11)
        await _write_old(store, pool, cid, "before index construction", 0)
        await bf._prepare(pool, _SCHEMA)
        await bf._pass(pool, _SCHEMA, None)
        protected = await _clock(pool)
        build = None
        try:
            async with pool.acquire() as writer:
                async with writer.transaction():
                    await _posting_write(store, writer, cid, "changed during index construction")
                    build = asyncio.create_task(bf._build_index(pool, _SCHEMA))
                    # Hold a real posting writer open until CREATE INDEX
                    # CONCURRENTLY visibly waits for it; no timing-only race.
                    async with asyncio.timeout(10):
                        while True:
                            async with pool.acquire() as observer:
                                waiting = await observer.fetchval(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_progress_create_index "
                                    "WHERE relid='vector_index.chunks'::regclass "
                                    "AND phase LIKE 'waiting for writers%')"
                                )
                            if waiting:
                                break
                            if build.done():
                                await build
                                pytest.fail("index build never overlapped the writer")
                            await asyncio.sleep(0.01)
                # Commit releases the actual build barrier.
            await asyncio.wait_for(build, timeout=10)
            async with pool.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT indisvalid FROM pg_index WHERE indexrelid="
                    "'vector_index.idx_vi_chunks_bm25'::regclass"
                )
            before_fix = await _body_vector(pool, cid)
            expected = await _expected(pool, before_fix["content"])
            assert before_fix["vector"] != expected
            # A flip-time watermark misses this already committed write.
            flip_time = await _clock(pool)
            assert await bf._counts(pool, _SCHEMA, flip_time) == (0, 0)
            assert await bf._pass(pool, _SCHEMA, protected) == 1
            assert (await _body_vector(pool, cid))["vector"] == expected
        finally:
            if build is not None and not build.done():
                build.cancel()
                await asyncio.gather(build, return_exceptions=True)


async def test_cancelled_concurrent_index_requires_drop_before_rebuild(monkeypatch):
    """Cancel real DDL at its writer barrier, not a synthetic pg_index edit."""
    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=17)
        await _write_old(store, pool, cid, "index cancellation fixture", 0)
        await bf._prepare(pool, _SCHEMA)
        await bf._pass(pool, _SCHEMA, None)
        build = None
        try:
            async with pool.acquire() as writer:
                async with writer.transaction():
                    await _posting_write(store, writer, cid, "writer holds index barrier")
                    build = asyncio.create_task(bf._build_index(pool, _SCHEMA))
                    async with asyncio.timeout(10):
                        while True:
                            async with pool.acquire() as observer:
                                waiting = await observer.fetchval(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_progress_create_index "
                                    "WHERE relid='vector_index.chunks'::regclass "
                                    "AND phase LIKE 'waiting for writers%')"
                                )
                            if waiting:
                                break
                            if build.done():
                                await build
                                pytest.fail("index build never reached writer barrier")
                            await asyncio.sleep(0.01)
                    build.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await build
                # Commit/drain the writer before inspecting or recovering DDL.
            async with pool.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT indisvalid FROM pg_index WHERE indexrelid="
                    "to_regclass('vector_index.idx_vi_chunks_bm25')"
                ) is False, "cancelled concurrent DDL must leave a real invalid index"
            with pytest.raises(SystemExit, match="INVALID"):
                await bf._build_index(pool, _SCHEMA)
            async with pool.acquire() as conn:
                await conn.execute(
                    'DROP INDEX CONCURRENTLY vector_index.idx_vi_chunks_bm25'
                )
                assert await conn.fetchval(
                    "SELECT to_regclass('vector_index.idx_vi_chunks_bm25')"
                ) is None
            await bf._build_index(pool, _SCHEMA)
            async with pool.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT indisvalid FROM pg_index WHERE indexrelid="
                    "'vector_index.idx_vi_chunks_bm25'::regclass"
                ) is True
        finally:
            if build is not None and not build.done():
                build.cancel()
                await asyncio.gather(build, return_exceptions=True)
