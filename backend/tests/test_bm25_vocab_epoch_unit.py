"""The vocabulary epoch, as the encoder and the writers use it (akb#687).

Term ids are renumbered by `scripts/compact_bm25_term_ids.py`, in one
transaction that also advances `bm25_vocab_epoch`. Encoding a chunk and storing
it are two transactions, so both sides carry the epoch:

- a lookup that spans more than one statement reads the epoch again at the end
  and starts over if it moved, so it never returns ids from two numberings;
- a writer re-reads the epoch under the term-id fence, in the transaction that
  stores the ids, and refuses ids read under another epoch.

These tests script the connection statement by statement, because the order is
the contract: the fence must be held before the epoch is read, and the ids must
come from the same snapshot as the epoch they are reported with.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.services import sparse_encoder
from app.services.bm25_maintenance import BM25_VOCAB_EPOCH_LOCK_KEY

pytestmark = pytest.mark.asyncio


class _ScriptedConnection:
    """Answers each statement from a script, and fails on anything unscripted.

    Each step is (method, a fragment the SQL must contain, the answer).
    """

    def __init__(self, *steps):
        self.steps = list(steps)
        self.statements: list[tuple[str, str, tuple]] = []

    async def _answer(self, method, sql, args):
        self.statements.append((method, sql, args))
        assert self.steps, f"unscripted {method}: {sql}"
        expected_method, fragment, answer = self.steps.pop(0)
        assert method == expected_method, (method, sql)
        assert fragment in sql, (fragment, sql)
        return answer

    async def fetchval(self, sql, *args):
        return await self._answer("fetchval", sql, args)

    async def fetchrow(self, sql, *args):
        return await self._answer("fetchrow", sql, args)

    async def fetch(self, sql, *args):
        return await self._answer("fetch", sql, args)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _use(monkeypatch, conn):
    async def get_pool():
        return _Pool(conn)

    monkeypatch.setattr(sparse_encoder, "get_pool", get_pool)


_FENCE = ("fetchval", "pg_try_advisory_xact_lock_shared", True)


def _known(epoch, ids):
    """The lookup statement: the epoch and the known ids, from one snapshot."""
    return ("fetchrow", "FROM bm25_vocab WHERE term = ANY", {
        "epoch": epoch,
        "terms": list(ids) or None,
        "ids": list(ids.values()) or None,
    })


def _inserted(ids):
    return ("fetch", "INSERT INTO bm25_vocab", [{"term": t, "term_id": i} for t, i in ids.items()])


def _epoch(value):
    return ("fetchval", "FROM bm25_vocab_epoch", value)


async def test_a_lookup_answered_by_one_statement_needs_no_second_read(monkeypatch):
    conn = _ScriptedConnection(_FENCE, _known(4, {"alpha": 900, "beta": 1_000_001}))
    _use(monkeypatch, conn)

    ids, epoch = await sparse_encoder.get_or_create_term_ids_at_epoch(["beta", "alpha", ""])

    assert (ids, epoch) == ({"alpha": 900, "beta": 1_000_001}, 4)
    assert conn.steps == []


async def test_a_lookup_that_straddles_a_renumbering_is_read_again(monkeypatch):
    """New terms take three statements; a renumbering can commit between them.

    `beta` is inserted with an id from the old numbering while `alpha` was read
    from it, and then the epoch has moved: neither id may be returned.
    """
    conn = _ScriptedConnection(
        _FENCE,
        _known(4, {"alpha": 900}),
        _inserted({"beta": 1_000_001}),
        _epoch(5),
        _FENCE,
        _known(5, {"alpha": 0, "beta": 1}),
    )
    _use(monkeypatch, conn)

    ids, epoch = await sparse_encoder.get_or_create_term_ids_at_epoch(["alpha", "beta"])

    assert (ids, epoch) == ({"alpha": 0, "beta": 1}, 5)
    assert conn.steps == []


async def test_new_terms_under_a_steady_epoch_are_returned_with_it(monkeypatch):
    conn = _ScriptedConnection(
        _FENCE,
        _known(4, {"alpha": 900}),
        _inserted({"beta": 1_000_001}),
        _epoch(4),
    )
    _use(monkeypatch, conn)

    assert await sparse_encoder.get_or_create_term_ids_at_epoch(["alpha", "beta"]) == (
        {"alpha": 900, "beta": 1_000_001}, 4,
    )


async def test_a_lookup_that_never_settles_gives_up(monkeypatch):
    attempt = [_FENCE, _known(1, {}), _inserted({"alpha": 7}), _epoch(2)]
    conn = _ScriptedConnection(*(attempt * sparse_encoder._VOCABULARY_EPOCH_ATTEMPTS))
    _use(monkeypatch, conn)

    with pytest.raises(RuntimeError, match="renumbered"):
        await sparse_encoder.get_or_create_term_ids_at_epoch(["alpha"])
    assert conn.steps == []


async def test_a_renumbering_in_progress_is_refused_before_the_vocabulary_is_read(monkeypatch):
    """The renumbering locks the vocabulary for its whole transaction.

    Checking the fence first turns what would be a lookup blocked until the
    statement timeout into an immediate, named refusal.
    """
    conn = _ScriptedConnection(("fetchval", "pg_try_advisory_xact_lock_shared", False))
    _use(monkeypatch, conn)

    with pytest.raises(sparse_encoder.VocabularyRenumberingInProgress):
        await sparse_encoder.get_or_create_term_ids_at_epoch(["alpha"])
    assert conn.statements[0][2] == (BM25_VOCAB_EPOCH_LOCK_KEY,)


async def test_no_terms_touch_no_vocabulary(monkeypatch):
    conn = _ScriptedConnection()
    _use(monkeypatch, conn)

    assert await sparse_encoder.get_or_create_term_ids_at_epoch(["", ""]) == ({}, None)
    assert await sparse_encoder.get_or_create_term_ids([]) == {}


async def test_the_encoding_carries_the_epoch_of_its_ids(monkeypatch):
    monkeypatch.setattr(sparse_encoder, "tokenize", AsyncMock(return_value=["b", "a", "b"]))
    monkeypatch.setattr(
        sparse_encoder, "get_or_create_term_ids_at_epoch",
        AsyncMock(return_value=({"a": 3, "b": 9}, 7)),
    )

    encoded = await sparse_encoder.encode_document_at_epoch("text", sparse_shape="vchord")

    assert dict(zip(encoded.indices, encoded.values)) == {3: 1.0, 9: 2.0}
    assert encoded.epoch == 7


async def test_a_document_without_terms_has_no_epoch(monkeypatch):
    monkeypatch.setattr(sparse_encoder, "tokenize", AsyncMock(return_value=[]))

    assert await sparse_encoder.encode_document_at_epoch("", sparse_shape="vchord") == (
        sparse_encoder.EncodedDocument([], [], None)
    )


async def test_a_prebaked_query_is_read_again_when_the_epoch_moves(monkeypatch):
    """Ids and their df are two statements for the pre-baked shapes.

    A renumbering between them would weight each term with another term's df.
    """
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_driver", "pgvector")
    monkeypatch.setattr(sparse_encoder, "tokenize", AsyncMock(return_value=["alpha"]))
    monkeypatch.setattr(sparse_encoder, "vocabulary_epoch", AsyncMock(side_effect=[3, 4, 4, 4]))
    lookup = AsyncMock(side_effect=[{"alpha": 900}, {"alpha": 0}])
    monkeypatch.setattr(sparse_encoder, "lookup_term_ids", lookup)
    df = AsyncMock(side_effect=[{900: 5}, {0: 5}])
    monkeypatch.setattr(sparse_encoder, "load_df_for_terms", df)
    monkeypatch.setattr(sparse_encoder, "load_stats", AsyncMock(return_value={"total_docs": 100}))

    indices, values = await sparse_encoder.encode_query("alpha", sparse_shape="posting")

    assert indices == [0] and values == [pytest.approx(sparse_encoder._idf(5, 100))]
    assert lookup.await_count == 2


async def test_a_raw_query_is_one_statement_and_reads_no_epoch(monkeypatch):
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_driver", "pgvector")
    monkeypatch.setattr(sparse_encoder, "tokenize", AsyncMock(return_value=["alpha"]))
    epoch = AsyncMock(side_effect=AssertionError("one statement is one snapshot"))
    monkeypatch.setattr(sparse_encoder, "vocabulary_epoch", epoch)
    monkeypatch.setattr(sparse_encoder, "lookup_term_ids", AsyncMock(return_value={"alpha": 0}))

    assert await sparse_encoder.encode_query("alpha", sparse_shape="vchord") == ([0], [1.0])


async def test_the_fence_is_held_before_the_epoch_is_read():
    """Two statements, in this order, and it matters.

    Read committed gives each statement its own snapshot. An epoch read in the
    statement that takes the lock would use a snapshot taken before the lock
    was granted — before a renumbering that was holding it committed — and
    would pass stale ids.
    """
    conn = _ScriptedConnection(_FENCE, _epoch(3))

    await sparse_encoder.hold_vocabulary_epoch(conn, 3)

    (lock, lock_sql, lock_args), (read, read_sql, _) = conn.statements
    assert (lock, read) == ("fetchval", "fetchval")
    assert lock_args == (BM25_VOCAB_EPOCH_LOCK_KEY,)
    assert "bm25_vocab_epoch" not in lock_sql
    assert "pg_try_advisory" not in read_sql


async def test_the_fence_refuses_ids_from_another_epoch():
    conn = _ScriptedConnection(_FENCE, _epoch(4))

    with pytest.raises(sparse_encoder.VocabularyEpochMoved) as refused:
        await sparse_encoder.hold_vocabulary_epoch(conn, 3)

    assert (refused.value.encoded, refused.value.current) == (3, 4)
    assert "epoch 3" in str(refused.value) and "4" in str(refused.value)


async def test_the_fence_refuses_while_a_renumbering_holds_it():
    conn = _ScriptedConnection(("fetchval", "pg_try_advisory_xact_lock_shared", False))

    with pytest.raises(sparse_encoder.VocabularyRenumberingInProgress):
        await sparse_encoder.hold_vocabulary_epoch(conn, 3)
    assert len(conn.statements) == 1


async def test_an_encoding_without_ids_needs_no_fence():
    conn = _ScriptedConnection()

    await sparse_encoder.hold_vocabulary_epoch(conn, None)

    assert conn.statements == []
