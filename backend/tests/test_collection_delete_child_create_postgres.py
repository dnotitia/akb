"""A recursive collection delete and a sub-collection created under its
prefix at the same time.

The delete reads the collections at and under its prefix, locks them, and reads
them again; then it reads the documents, files and tables under the prefix and
deletes them with the locked collections. A sub-collection whose creation had
not committed at the second read, but committed before the documents were
read, was missed: the document created with it was deleted and the collection
stayed, empty, with a count of one. A creation now locks its existing ancestor
collections ``FOR KEY SHARE`` (the delete locks them ``FOR UPDATE``), and the
delete reads the collections once more after the documents, files and tables,
which also covers a prefix with no row of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from pathlib import Path

import asyncpg
import pytest

import app.services.document_service as document_service_module
from app.models.document import DocumentPutRequest
from app.repositories.document_repo import CollectionRepository
from app.services.collection_service import CollectionService
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from tests.test_document_git_event_lock_postgres import _until_blocked
from tests.test_events_commit_order_postgres import (
    _EVENT_LOCK_WAIT_SECONDS,
    _OpenTransaction,
    _fresh_database,
    _make_vault,
)

_TIMEOUT = _EVENT_LOCK_WAIT_SECONDS * 4


async def _collections(pool, vault_id: uuid.UUID) -> list[tuple[str, int, int]]:
    """Each collection with its stored count and the documents it holds."""
    rows = await pool.fetch(
        "SELECT c.path, c.doc_count, "
        "       (SELECT count(*) FROM documents d WHERE d.collection_id = c.id) AS documents "
        "  FROM collections c WHERE c.vault_id = $1 ORDER BY c.path",
        vault_id,
    )
    return [(r["path"], r["doc_count"], r["documents"]) for r in rows]


async def _blocked_by(observer, pid: int) -> bool:
    return bool(await observer.fetchval(
        "SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))", pid,
    ))


async def _cancel(*tasks: asyncio.Task | None) -> None:
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


@pytest.mark.parametrize("target", ["row", "no row"])
async def test_a_sub_collection_created_during_a_recursive_delete_does_not_stay(
    tmp_path, monkeypatch, target,
):
    """A put into a new collection `p/new` is held before its commit; the
    recursive delete of `p` either waits for it or reaches its document read;
    the put commits; the delete finishes. Nothing may remain under `p`. With a
    row at `p` the put's lock on it holds the delete back; without one the
    delete's read after the documents finds `p/new` and starts over."""
    git = GitService(storage_path=str(tmp_path))
    documents = DocumentService(git=git)
    collections = CollectionService(git=git)
    async with _fresh_database() as pool:
        vault = "child-create-" + target.replace(" ", "-")
        vault_id = await _make_vault(pool, vault)
        git.init_vault(vault)
        if target == "row":
            await pool.execute(
                "INSERT INTO collections (id, vault_id, path, name) VALUES ($1, $2, 'p', 'p')",
                uuid.uuid4(), vault_id,
            )
        await documents.put(DocumentPutRequest(
            vault=vault, collection="p/old", title="Old", content="old body",
        ))

        # Hold the put before its event lock and Git commit, with its new
        # collection inserted.
        lock_vault_events = document_service_module.lock_vault_events
        put_held = asyncio.Event()
        release_put = asyncio.Event()
        put_pid: int | None = None

        async def holding_lock_vault_events(conn, vault_uuid):
            nonlocal put_pid
            if put_pid is None:
                put_pid = conn.get_server_pid()
                put_held.set()
                await release_put.wait()
            return await lock_vault_events(conn, vault_uuid)

        monkeypatch.setattr(document_service_module, "lock_vault_events", holding_lock_vault_events)

        # Hold the delete at its first document read until the put committed.
        list_docs_under = CollectionRepository.list_docs_under
        delete_reading = asyncio.Event()
        put_committed = asyncio.Event()

        async def holding_list_docs_under(self, *args, **kwargs):
            if not delete_reading.is_set():
                delete_reading.set()
                await put_committed.wait()
            return await list_docs_under(self, *args, **kwargs)

        monkeypatch.setattr(CollectionRepository, "list_docs_under", holding_list_docs_under)

        putting = asyncio.create_task(documents.put(DocumentPutRequest(
            vault=vault, collection="p/new", title="Raced", content="raced body",
        )))
        deleting: asyncio.Task | None = None
        async with pool.acquire() as observer:
            try:
                await asyncio.wait_for(put_held.wait(), timeout=10)
                assert put_pid is not None
                deleting = asyncio.create_task(collections.delete(
                    vault=vault, path="p", recursive=True, agent_id="tester",
                ))

                async def reading_or_waiting() -> str:
                    while True:
                        if delete_reading.is_set():
                            return "reading"
                        if await _blocked_by(observer, put_pid):
                            return "waiting"
                        await asyncio.sleep(0.01)

                reached = await asyncio.wait_for(reading_or_waiting(), timeout=10)
                release_put.set()
                doc = await asyncio.wait_for(putting, timeout=_TIMEOUT)
                put_committed.set()
                result = await asyncio.wait_for(deleting, timeout=_TIMEOUT)
            finally:
                release_put.set()
                put_committed.set()
                await _cancel(putting, deleting)

        assert await _collections(pool, vault_id) == []
        assert await pool.fetchval("SELECT count(*) FROM documents WHERE vault_id = $1", vault_id) == 0
        assert (result["deleted_docs"], result["deleted_sub_collections"]) == (2, 2)
        assert git.read_file(vault, doc.path) is None
        # With a row at `p` the put's lock on it holds the delete back.
        assert reached == ("waiting" if target == "row" else "reading")


async def _await_both(*tasks: asyncio.Task) -> list[object]:
    return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=_TIMEOUT)


async def test_a_move_into_a_new_sub_collection_and_a_recursive_delete_do_not_deadlock(
    tmp_path, monkeypatch,
):
    """A move from `p/a` into a new `p/x` locks `p/x`'s ancestor `p` and its
    source `p/a`; a recursive delete of `p` locks `p/a` and then `p` (id
    order). Locking the ancestor first and the source afterwards, the move
    would hold `p` while it waited for `p/a`, and the delete the reverse. The
    move locks both in one id-ordered pass when it creates `p/x`."""
    parent = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
    source = uuid.UUID("00000000-0000-4000-8000-000000000001")
    git = GitService(storage_path=str(tmp_path))
    documents = DocumentService(git=git)
    collections = CollectionService(git=git)
    async with _fresh_database() as pool:
        vault = "child-create-move"
        vault_id = await _make_vault(pool, vault)
        git.init_vault(vault)
        await pool.execute(
            "INSERT INTO collections (id, vault_id, path, name) "
            "VALUES ($1, $2, 'p', 'p'), ($3, $2, 'p/a', 'a')",
            parent, vault_id, source,
        )
        doc = await documents.put(DocumentPutRequest(
            vault=vault, collection="p/a", title="Moved", content="moved body",
        ))

        # Hold the move right after it created `p/x`, before its next lock.
        lock_rows = document_service_module.lock_collection_rows
        move_held = asyncio.Event()
        release_move = asyncio.Event()
        move_pid: int | None = None

        async def holding_lock_rows(conn, collection_ids, **kwargs):
            nonlocal move_pid
            if move_pid is None:
                move_pid = conn.get_server_pid()
                move_held.set()
                await release_move.wait()
            return await lock_rows(conn, collection_ids, **kwargs)

        monkeypatch.setattr(document_service_module, "lock_collection_rows", holding_lock_rows)

        moving = asyncio.create_task(documents.move(vault, doc.path, collection="p/x"))
        deleting: asyncio.Task | None = None
        async with pool.acquire() as observer:
            try:
                await asyncio.wait_for(move_held.wait(), timeout=10)
                assert move_pid is not None
                deleting = asyncio.create_task(collections.delete(
                    vault=vault, path="p", recursive=True, agent_id="tester",
                ))

                async def delete_waits_for_the_move() -> None:
                    while not await _blocked_by(observer, move_pid):
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(delete_waits_for_the_move(), timeout=10)
                release_move.set()
                moved, deleted = await _await_both(moving, deleting)
            finally:
                release_move.set()
                await _cancel(moving, deleting)

        assert not isinstance(moved, BaseException), repr(moved)
        assert not isinstance(deleted, BaseException), repr(deleted)
        # The delete waited for the move and then deleted `p/x` with it.
        assert moved.path.startswith("p/x/")
        assert (deleted["deleted_docs"], deleted["deleted_sub_collections"]) == (1, 2)
        assert await _collections(pool, vault_id) == []


async def test_a_creation_locks_its_ancestors_in_id_order(tmp_path):
    """A put into a new `p/q/new` locks the ancestors `p` and `p/q`; `p/q` has
    the lower id. A recursive delete of `p` holds `p/q` and waits for `p`,
    which a gate holds; the put then waits for `p/q`. Locking `p` first, the
    put would hold it once the gate left and wait for the delete, which waits
    for `p`. In id order the put holds nothing the delete needs: the delete
    finishes and the put creates `p/q/new` afterwards."""
    upper = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
    lower = uuid.UUID("00000000-0000-4000-8000-000000000001")
    git = GitService(storage_path=str(tmp_path))
    documents = DocumentService(git=git)
    collections = CollectionService(git=git)
    async with _fresh_database() as pool:
        vault = "child-create-deep"
        vault_id = await _make_vault(pool, vault)
        git.init_vault(vault)
        await pool.execute(
            "INSERT INTO collections (id, vault_id, path, name) "
            "VALUES ($1, $2, 'p', 'p'), ($3, $2, 'p/q', 'q')",
            upper, vault_id, lower,
        )
        putting: asyncio.Task | None = None
        deleting: asyncio.Task | None = None
        async with pool.acquire() as gate, pool.acquire() as observer:
            try:
                async with _OpenTransaction(gate):
                    # Holds the delete at `p`, and lets a FOR KEY SHARE through.
                    await gate.execute("SELECT 1 FROM collections WHERE id = $1 FOR NO KEY UPDATE", upper)
                    deleting = asyncio.create_task(collections.delete(
                        vault=vault, path="p", recursive=True, agent_id="tester",
                    ))
                    await asyncio.wait_for(_until_blocked(observer, 1), timeout=10)
                    putting = asyncio.create_task(documents.put(DocumentPutRequest(
                        vault=vault, collection="p/q/new", title="Deep", content="deep body",
                    )))
                    await asyncio.wait_for(_until_blocked(observer, 2), timeout=10)
                # The gate is gone; the delete gets `p`.
                deleted, put = await _await_both(deleting, putting)
            finally:
                await _cancel(deleting, putting)

        assert not isinstance(deleted, BaseException), repr(deleted)
        assert not isinstance(put, BaseException), repr(put)
        assert deleted["deleted_sub_collections"] == 1
        # The put created `p/q/new` after the delete, whole.
        assert await _collections(pool, vault_id) == [("p/q/new", 1, 1)]
        assert "deep body" in git.read_file(vault, put.path)


@pytest.mark.parametrize("entry", ["get_or_create", "create_empty"])
async def test_creating_a_collection_locks_its_ancestors_until_commit(entry):
    """Both repository entry points lock the existing ancestors of a row they
    insert, and only of a row they insert."""
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "child-create-entry")
        ancestors = {}
        for path in ("a", "a/b"):
            ancestors[path] = await pool.fetchval(
                "INSERT INTO collections (id, vault_id, path, name) VALUES ($1, $2, $3, $3) RETURNING id",
                uuid.uuid4(), vault_id, path,
            )
        repo = CollectionRepository(pool)

        async def create(conn, path):
            if entry == "get_or_create":
                return await repo.get_or_create(vault_id, path, conn=conn)
            return (await repo.create_empty(vault_id, path, conn=conn))[0]

        async def locked(observer) -> set[str]:
            """The ancestors a FOR UPDATE NOWAIT cannot take."""
            held = set()
            for path, cid in ancestors.items():
                try:
                    async with observer.transaction():
                        await observer.execute("SELECT 1 FROM collections WHERE id = $1 FOR UPDATE NOWAIT", cid)
                except asyncpg.LockNotAvailableError:
                    held.add(path)
            return held

        async with pool.acquire() as creator, pool.acquire() as observer:
            async with _OpenTransaction(creator) as creating:
                await create(creator, "a/b/c")
                assert await locked(observer) == {"a", "a/b"}
                await creating.commit()
            assert await locked(observer) == set()
            async with _OpenTransaction(creator):
                # The row exists now: no insert, no ancestor lock.
                await create(creator, "a/b/c")
                assert await locked(observer) == set()


def test_collections_are_inserted_only_through_the_repository():
    """Every insert into `collections` locks the new row's ancestors, which
    the repository does; another insert would skip that."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    inserting = re.compile(r"INSERT\s+INTO\s+(?:public\.)?collections\b", re.IGNORECASE)
    found = sorted(
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if "migrations" not in path.parts and inserting.search(path.read_text(encoding="utf-8"))
    )
    assert found == ["repositories/document_repo.py"]
