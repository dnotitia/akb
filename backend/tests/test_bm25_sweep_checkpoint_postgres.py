"""Crash and ownership boundaries for the DB-owned BM25 sweep cursor.

The fixture creates a disposable database on AKB_VCHORD_TEST_DSN. No shared
corpus or serving endpoint is touched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from scripts import backfill_bm25_vector as bf
from scripts.bm25_sweep_checkpoint import open_checkpoint
from tests.test_bm25_transition_boundary_postgres import (
    _body_vector,
    _expected,
    _posting_write,
)
from tests.test_bm25_vector_backfill_postgres import _SCHEMA, _corpus, _write_old

pytestmark = pytest.mark.asyncio

_SINCE = datetime(2020, 1, 1, tzinfo=timezone.utc)
_PROTECT = datetime(2020, 1, 2, tzinfo=timezone.utc)
_SOURCE = "a" * 40
_CONTRACT = "encoder-v1"


async def _open(pool, sweep, attempt, *, resume=False, **changes):
    args = dict(
        sweep_id=sweep, attempt_id=attempt, since=_SINCE,
        protect_since=_PROTECT, source_revision=_SOURCE,
        encoding_contract=_CONTRACT, resume=resume,
    )
    args.update(changes)
    return await open_checkpoint(pool, _SCHEMA, **args)


async def test_checkpoint_advances_only_past_committed_writer_waves(monkeypatch):
    async with _corpus(monkeypatch) as (store, pool):
        ids = [uuid.UUID(int=i) for i in range(1, 5)]
        for i, cid in enumerate(ids):
            await _write_old(store, pool, cid, f"row {i}", i)
        await bf._prepare(pool, _SCHEMA)
        sweep, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        receipt = await _open(pool, sweep, first)
        real_apply = bf._apply

        async def fail_second_wave(pool_, schema, rows):
            if rows[0]["chunk_id"] == ids[2]:
                raise RuntimeError("one writer failed after the first wave")
            return await real_apply(pool_, schema, rows)

        with monkeypatch.context() as patch:
            patch.setattr(bf, "_apply", fail_second_wave)
            with pytest.raises(RuntimeError, match="one writer failed"):
                await bf._pass(
                    pool, _SCHEMA, _SINCE, writers=2,
                    write_batch_size=1, checkpoint=receipt,
                )
        # A sibling in the second wave may have committed. The durable cursor
        # must still be the end of the first *fully completed* wave.
        assert receipt.cursor == ids[1]
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                'SELECT last_chunk_id FROM vector_index.bm25_backfill_checkpoint '
                'WHERE sweep_id=$1', sweep,
            ) == ids[1]

        resumed = await _open(pool, sweep, second, resume=True)
        assert resumed.cursor == ids[1]
        assert await bf._pass(
            pool, _SCHEMA, _SINCE, writers=2,
            write_batch_size=1, checkpoint=resumed,
        ) == 2
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT last_chunk_id, exhausted, seen, written '
                'FROM vector_index.bm25_backfill_checkpoint WHERE sweep_id=$1',
                sweep,
            )
            nulls = await conn.fetchval(
                'SELECT count(*) FROM vector_index.chunks WHERE sparse_bm25 IS NULL'
            )
        assert row["last_chunk_id"] == ids[-1]
        assert row["exhausted"] is True
        assert row["seen"] == 4
        assert row["written"] == 4
        assert nulls == 0


async def test_written_wave_without_checkpoint_receipt_replays(monkeypatch):
    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=11)
        await _write_old(store, pool, cid, "already committed", 0)
        await bf._prepare(pool, _SCHEMA)
        sweep = uuid.uuid4()
        receipt = await _open(pool, sweep, uuid.uuid4())

        async def lost_receipt(*_args):
            raise RuntimeError("checkpoint connection lost after vector commit")

        with monkeypatch.context() as patch:
            patch.setattr(receipt, "advance", lost_receipt)
            with pytest.raises(RuntimeError, match="checkpoint connection lost"):
                await bf._pass(pool, _SCHEMA, _SINCE, checkpoint=receipt)
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                'SELECT sparse_bm25 IS NOT NULL FROM vector_index.chunks '
                'WHERE chunk_id=$1', cid,
            ) is True
            assert await conn.fetchval(
                'SELECT last_chunk_id FROM vector_index.bm25_backfill_checkpoint '
                'WHERE sweep_id=$1', sweep,
            ) == uuid.UUID(int=0)
        resumed = await _open(pool, sweep, uuid.uuid4(), resume=True)
        assert await bf._pass(pool, _SCHEMA, _SINCE, checkpoint=resumed) == 1
        assert resumed.cursor == cid


async def test_committed_checkpoint_survives_lost_acknowledgement(monkeypatch):
    async with _corpus(monkeypatch) as (store, pool):
        cid = uuid.UUID(int=17)
        await _write_old(store, pool, cid, "one completed wave", 0)
        await bf._prepare(pool, _SCHEMA)
        sweep = uuid.uuid4()
        receipt = await _open(pool, sweep, uuid.uuid4())
        real_advance = receipt.advance

        async def lost_ack(*args):
            await real_advance(*args)
            raise RuntimeError("reply lost after checkpoint commit")

        with monkeypatch.context() as patch:
            patch.setattr(receipt, "advance", lost_ack)
            with pytest.raises(RuntimeError, match="reply lost"):
                await bf._pass(pool, _SCHEMA, _SINCE, checkpoint=receipt)
        resumed = await _open(pool, sweep, uuid.uuid4(), resume=True)
        assert resumed.cursor == cid
        assert resumed.seen == 1
        assert resumed.written == 1
        assert await bf._pass(pool, _SCHEMA, _SINCE, checkpoint=resumed) == 0


async def test_resume_does_not_replace_protected_catch_up(monkeypatch):
    async with _corpus(monkeypatch) as (store, pool):
        ids = [uuid.UUID(int=31), uuid.UUID(int=32)]
        for i, cid in enumerate(ids):
            await _write_old(store, pool, cid, f"old {i}", i)
        await bf._prepare(pool, _SCHEMA)
        sweep = uuid.uuid4()
        receipt = await _open(pool, sweep, uuid.uuid4())
        real_apply = bf._apply

        async def stop_after_first_wave(pool_, schema, rows):
            if rows[0]["chunk_id"] == ids[1]:
                raise RuntimeError("stop between waves")
            return await real_apply(pool_, schema, rows)

        with monkeypatch.context() as patch:
            patch.setattr(bf, "_apply", stop_after_first_wave)
            with pytest.raises(RuntimeError, match="stop between waves"):
                await bf._pass(
                    pool, _SCHEMA, _SINCE, writers=1,
                    write_batch_size=1, checkpoint=receipt,
                )
        assert receipt.cursor == ids[0]
        async with pool.acquire() as writer:
            async with writer.transaction():
                await _posting_write(store, writer, ids[0], "replacement below cursor")
        before = await _body_vector(pool, ids[0])
        assert before["vector"] != await _expected(pool, before["content"])

        resumed = await _open(pool, sweep, uuid.uuid4(), resume=True)
        assert await bf._pass(
            pool, _SCHEMA, _SINCE, writers=1,
            write_batch_size=1, checkpoint=resumed,
        ) == 1
        still_stale = await _body_vector(pool, ids[0])
        assert still_stale["vector"] == before["vector"]
        # This separate cursor-zero pass is mandatory after old writers drain.
        assert await bf._pass(pool, _SCHEMA, _PROTECT) == 2
        fixed = await _body_vector(pool, ids[0])
        assert fixed["vector"] == await _expected(pool, fixed["content"])


async def test_checkpoint_contract_and_attempt_are_fenced(monkeypatch):
    async with _corpus(monkeypatch) as (_store, pool):
        await bf._prepare(pool, _SCHEMA)
        sweep, first = uuid.uuid4(), uuid.uuid4()
        old = await _open(pool, sweep, first)
        with pytest.raises(RuntimeError, match="already exists"):
            await _open(pool, sweep, uuid.uuid4())
        with pytest.raises(RuntimeError, match="contract changed"):
            await _open(
                pool, sweep, uuid.uuid4(), resume=True,
                protect_since=datetime(2020, 1, 3, tzinfo=timezone.utc),
            )
        with pytest.raises(RuntimeError, match="contract changed"):
            await _open(pool, sweep, uuid.uuid4(), resume=True,
                        source_revision="b" * 40)
        with pytest.raises(RuntimeError, match="contract changed"):
            await _open(pool, sweep, uuid.uuid4(), resume=True,
                        encoding_contract="different encoder")
        with pytest.raises(RuntimeError, match="new attempt ID"):
            await _open(pool, sweep, first, resume=True)
        newer = await _open(pool, sweep, uuid.uuid4(), resume=True)
        with pytest.raises(RuntimeError, match="ownership"):
            await old.advance(uuid.UUID(int=1), 1, 0)
        assert newer.cursor == uuid.UUID(int=0)
