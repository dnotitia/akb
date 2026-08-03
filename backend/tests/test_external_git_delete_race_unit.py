"""External_git tombstone delete race-safety.

No network, no real DB: ``ExternalGitService._delete_external_path`` is driven
with a fake pool/connection that models one ``documents`` row plus the
``FOR UPDATE`` lock + ``DELETE ... RETURNING`` contract. This asserts the fix in
isolation — the collection decrement and the ``document.delete`` event fire
ONLY when a row is actually deleted (never off a stale pre-transaction read), so
two overlapping reconciles (a claim lease that expired and let a second run
start) can't double-decrement or emit a duplicate event.
"""

from __future__ import annotations

import uuid

import pytest

import app.services.kg_service as kg
from app.services import external_git_service as egs


class _FakeState:
    def __init__(self, row):
        self.row = row  # current documents row dict, or None once deleted
        self.select_sqls: list[str] = []
        self.chunk_deletes: list[str] = []
        self.relation_deletes: list[tuple[str, str]] = []
        self.decrements: list = []
        self.events: list[str] = []


class _FakeConn:
    def __init__(self, state, *, in_tx_flag):
        self.state = state
        self._in_tx = in_tx_flag  # single-element list → mutable "are we in a tx"

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if "FOR UPDATE" in s:
            # The lock/read MUST happen inside the transaction, not before it.
            assert self._in_tx[0], "FOR UPDATE lookup ran OUTSIDE the transaction"
            self.state.select_sqls.append(s)
            return dict(self.state.row) if self.state.row else None
        if s.startswith("DELETE FROM documents") and "RETURNING" in s:
            assert self._in_tx[0]
            row_id = args[0]
            if self.state.row is not None and self.state.row["id"] == row_id:
                self.state.row = None  # the row dies here (RETURNING → 1 row)
                return {"id": row_id}
            return None  # already gone → RETURNING yields nothing
        raise AssertionError(f"unexpected fetchrow SQL: {s}")

    async def execute(self, sql, *args):  # unused by the stubbed helpers
        return "OK"

    def transaction(self):
        flag = self._in_tx

        class _Tx:
            async def __aenter__(self_):
                flag[0] = True
                return None

            async def __aexit__(self_, *exc):
                flag[0] = False
                return False

        return _Tx()


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *exc):
                return False

        return _Acq()


class _FakeCollRepo:
    """Stands in for ``CollectionRepository`` — the module attribute is replaced
    with this instance, and ``CollectionRepository(pool)`` then calls it."""

    def __init__(self, state):
        self.state = state

    def __call__(self, pool):
        return self

    async def decrement_count(self, collection_id, now, conn=None):
        self.state.decrements.append(collection_id)


def _wire(monkeypatch, row):
    state = _FakeState(row)
    conn = _FakeConn(state, in_tx_flag=[False])
    pool = _FakePool(conn)

    async def _get_pool():
        return pool

    async def _chunks(c, doc_id):
        state.chunk_deletes.append(doc_id)

    async def _relations(c, vault_name, doc_path):
        state.relation_deletes.append((vault_name, doc_path))

    async def _emit(c, event_type, **kw):
        state.events.append(event_type)

    monkeypatch.setattr(egs, "get_pool", _get_pool)
    monkeypatch.setattr(egs, "delete_document_chunks", _chunks)
    monkeypatch.setattr(egs, "emit_event", _emit)
    monkeypatch.setattr(egs, "CollectionRepository", _FakeCollRepo(state))
    monkeypatch.setattr(kg, "delete_document_relations", _relations)
    return state


def _row(blob="b" * 40, coll_id="c-1"):
    return {
        "id": uuid.uuid4(),
        "collection_id": coll_id,
        "created_by": "external_git:host",
        "external_blob": blob,
    }


def _svc():
    # git=object() is truthy → no real GitService is constructed (no fs touch);
    # _delete_external_path never uses self.git.
    return egs.ExternalGitService(git=object())


@pytest.mark.asyncio
async def test_delete_external_path_normal_single_delete(monkeypatch):
    """A single tombstone deletes the row and fires the decrement + event ONCE,
    with the lookup locked (FOR UPDATE) inside the transaction."""
    row = _row()
    state = _wire(monkeypatch, row)
    outcome = await _svc()._delete_external_path(
        vault_id=uuid.uuid4(), vault_name="v", path="a.md",
        expected_blob=row["external_blob"],
    )
    assert outcome == "deleted"  # explicit outcome for the caller
    assert state.row is None  # deleted
    assert state.chunk_deletes == [str(row["id"])]
    assert state.relation_deletes == [("v", "a.md")]
    assert state.decrements == [row["collection_id"]]
    assert state.events == ["document.delete"]
    assert state.select_sqls and all("FOR UPDATE" in s for s in state.select_sqls)


@pytest.mark.asyncio
async def test_overlapping_tombstone_decrements_and_emits_exactly_once(monkeypatch):
    """Two overlapping reconciles both try to tombstone the same path. The first
    deletes it; the second's FOR UPDATE lookup returns no row → it must NOT
    decrement the count again or emit a duplicate delete event."""
    row = _row()
    state = _wire(monkeypatch, row)
    svc = _svc()
    outcomes = [
        await svc._delete_external_path(
            vault_id=uuid.uuid4(), vault_name="v", path="a.md",
            expected_blob=row["external_blob"],
        )
        for _ in range(2)
    ]
    # First deletes; the second finds no row → already_absent (a clean no-op, NOT
    # a conflict — nothing to reprocess).
    assert outcomes == ["deleted", "already_absent"]
    assert state.decrements == [row["collection_id"]]  # exactly one
    assert state.events == ["document.delete"]  # no duplicate
    assert state.chunk_deletes == [str(row["id"])]  # second call bailed early


@pytest.mark.asyncio
async def test_expected_blob_mismatch_skips_delete_and_side_effects(monkeypatch):
    """When a concurrent reconcile has re-indexed the path to a different blob,
    our (stale) snapshot's expected_blob no longer matches → the row is LEFT in
    place and no chunk delete / decrement / event happens."""
    row = _row(blob="a" * 40)
    state = _wire(monkeypatch, row)
    outcome = await _svc()._delete_external_path(
        vault_id=uuid.uuid4(), vault_name="v", path="a.md",
        expected_blob="z" * 40,  # snapshot blob ≠ the row's current blob
    )
    assert outcome == "conflict"  # retryable → caller holds the cursor
    assert state.row is not None  # a fresher version survives
    assert state.chunk_deletes == []
    assert state.decrements == []
    assert state.events == []


@pytest.mark.asyncio
async def test_missing_row_is_a_noop(monkeypatch):
    """No matching row (already tombstoned) → a clean no-op: no chunk delete,
    no decrement, no event."""
    state = _wire(monkeypatch, None)
    outcome = await _svc()._delete_external_path(
        vault_id=uuid.uuid4(), vault_name="v", path="gone.md", expected_blob="b" * 40,
    )
    assert outcome == "already_absent"  # clean no-op, not a conflict
    assert state.chunk_deletes == []
    assert state.decrements == []
    assert state.events == []


@pytest.mark.asyncio
async def test_delete_without_expected_blob_still_deletes(monkeypatch):
    """The expected_blob guard is optional — with None (defensive path) a present
    row is still tombstoned and the side effects fire once."""
    row = _row()
    state = _wire(monkeypatch, row)
    outcome = await _svc()._delete_external_path(
        vault_id=uuid.uuid4(), vault_name="v", path="a.md",
    )
    assert outcome == "deleted"
    assert state.row is None
    assert state.decrements == [row["collection_id"]]
    assert state.events == ["document.delete"]
