"""A Bare Git document write that cannot get its Vault's event lock fails
before it commits to Git.

A Git commit does not roll back with the database transaction. The writes used
to commit to Git and only then insert their event. When the Vault's event lock
stayed busy past its deadline, the transaction rolled back and the API answered
with a retryable ``write_busy``, while Git kept the commit: Git and the
database then disagreed about the document, and a later metadata-only update,
which reads the body from Git, could publish the body of the failed write.
Each Bare Git write now takes the lock before its Git commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import asyncpg
import pytest

from app.db.transient import transient_error
from app.exceptions import NotFoundError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.services.collection_service import CollectionService
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.util.errors import WRITE_BUSY
from tests.test_events_commit_order_postgres import (
    _EVENT_LOCK_WAIT_SECONDS,
    _OpenTransaction,
    _emit,
    _fresh_database,
    _make_vault,
)

pytestmark = pytest.mark.asyncio

_OPERATIONS = ("put", "update", "edit", "move", "delete")


async def _write(service: DocumentService, operation: str, vault: str, path: str, head: str) -> None:
    if operation == "put":
        await service.put(DocumentPutRequest(
            vault=vault, collection="notes", title="Second", content="second body",
        ))
    elif operation == "update":
        await service.update(vault, path, DocumentUpdateRequest(
            content="changed body", expected_commit=head,
        ))
    elif operation == "edit":
        await service.edit(vault, path, "first body", "edited body")
    elif operation == "move":
        await service.move(vault, path, collection="moved")
    else:
        await service.delete(vault, path)


async def test_a_busy_vault_fails_each_git_write_before_git_changes(tmp_path):
    git = GitService(storage_path=str(tmp_path))
    service = DocumentService(git=git)
    async with _fresh_database() as pool:
        prepared = {}
        for operation in _OPERATIONS:
            vault = f"git-lock-{operation}"
            vault_id = await _make_vault(pool, vault)
            git.init_vault(vault)
            # This first write also creates the Vault's lock row, as on any
            # Vault that already has events.
            doc = await service.put(DocumentPutRequest(
                vault=vault, collection="notes", title="First", content="first body",
            ))
            documents = await pool.fetch(
                "SELECT path, current_commit FROM documents WHERE vault_id = $1", vault_id,
            )
            events = await pool.fetchval("SELECT count(*) FROM events WHERE vault_id = $1", vault_id)
            prepared[operation] = (vault, vault_id, doc.path, git.current_commit(vault), documents, events)

        async def attempt(operation: str) -> tuple[BaseException | None, float]:
            vault, _, path, head, *_ = prepared[operation]
            started = time.monotonic()
            try:
                await _write(service, operation, vault, path, head)
            except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
                return exc, time.monotonic() - started
            return None, time.monotonic() - started

        async with contextlib.AsyncExitStack() as stack:
            for operation in _OPERATIONS:
                holder = await stack.enter_async_context(pool.acquire())
                await stack.enter_async_context(_OpenTransaction(holder))
                await _emit(holder, prepared[operation][1], "holder")
            results = await asyncio.wait_for(
                asyncio.gather(*(attempt(operation) for operation in _OPERATIONS)),
                timeout=_EVENT_LOCK_WAIT_SECONDS * 4,
            )
        # The holders rolled back; every Vault is as the first write left it.

        outcomes = {}
        for operation, (error, waited) in zip(_OPERATIONS, results):
            vault, vault_id, _, head, documents, events = prepared[operation]
            mapped = transient_error(error) if isinstance(error, Exception) else None
            outcomes[operation] = {
                "error": type(error).__name__ if error is not None else None,
                "api": (mapped.status_code, mapped.code) if mapped is not None else None,
                "waited for the deadline": waited >= _EVENT_LOCK_WAIT_SECONDS - 0.5,
                "git head unchanged": git.current_commit(vault) == head,
                "documents unchanged": await pool.fetch(
                    "SELECT path, current_commit FROM documents WHERE vault_id = $1", vault_id,
                ) == documents,
                "no new event": await pool.fetchval(
                    "SELECT count(*) FROM events WHERE vault_id = $1", vault_id,
                ) == events,
            }
        expected = {
            "error": "LockNotAvailableError",
            "api": (429, WRITE_BUSY),
            "waited for the deadline": True,
            "git head unchanged": True,
            "documents unchanged": True,
            "no new event": True,
        }
        assert outcomes == {operation: expected for operation in _OPERATIONS}


async def _a_backend_blocked_by(observer, holder) -> int:
    """The pid of a session waiting for a lock ``holder`` holds."""
    while True:
        pid = await observer.fetchval(
            "SELECT pid FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid)) LIMIT 1",
            holder.get_server_pid(),
        )
        if pid is not None:
            return pid
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("lock_row", ["exists", "missing"])
async def test_a_put_waits_for_a_collection_create_before_the_event_lock(tmp_path, lock_row):
    """A put into a collection that a `collection.create` has inserted but not
    committed waits for that row before it takes the Vault's event lock. The
    create then inserts its event without waiting, both commit, and Git and
    the database agree. Taking the event lock first, the put would make the
    create wait for it (a row) or deadlock with it (no row yet), with the put's
    Git commit already made."""
    git = GitService(storage_path=str(tmp_path))
    service = DocumentService(git=git)
    async with _fresh_database() as pool:
        vault = f"git-lock-race-{lock_row}"
        vault_id = await _make_vault(pool, vault)
        git.init_vault(vault)
        if lock_row == "exists":
            # A committed event: the Vault's lock row exists in either schema.
            async with pool.acquire() as conn, conn.transaction():
                await _emit(conn, vault_id, "earlier")
        else:
            await pool.execute("DELETE FROM vault_event_order WHERE vault_id = $1", vault_id)

        async with pool.acquire() as creator, pool.acquire() as observer:
            async with _OpenTransaction(creator) as creating:
                # `collection.create`: the collection's row, then its event.
                await CollectionRepository(pool).create_empty(vault_id, "notes", conn=creator)
                put = asyncio.create_task(service.put(DocumentPutRequest(
                    vault=vault, collection="notes", title="Raced", content="raced body",
                )))
                await asyncio.wait_for(_a_backend_blocked_by(observer, creator), timeout=10)
                started = time.monotonic()
                await emit_event(
                    creator, "collection.create", vault_id=vault_id,
                    payload={"vault": vault, "path": "notes", "created": True},
                )
                event_wait = time.monotonic() - started
                await creating.commit()
            doc = await asyncio.wait_for(put, timeout=_EVENT_LOCK_WAIT_SECONDS * 3)

        assert event_wait < 1.0, f"the collection event waited {event_wait:.2f}s"
        row = await pool.fetchrow("SELECT path, current_commit FROM documents WHERE vault_id = $1", vault_id)
        assert (row["path"], row["current_commit"]) == (doc.path, git.current_commit(vault))
        assert "raced body" in git.read_file(vault, doc.path)
        kinds = [r["kind"] for r in await pool.fetch(
            "SELECT kind FROM events WHERE vault_id = $1 ORDER BY id", vault_id,
        )]
        assert kinds[-2:] == ["collection.create", "document.put"]


async def test_a_lock_timeout_after_the_git_commit_is_not_write_busy(tmp_path):
    """Only the Vault event lock's own deadline is a retryable `write_busy`.
    Here a `lock_timeout` set on the database role ends an update while it
    waits for a chunk row after its Git commit: Git keeps that commit, so the
    error must not tell the client that nothing was written."""
    git = GitService(storage_path=str(tmp_path))
    service = DocumentService(git=git)
    async with _fresh_database() as pool:
        vault = "git-lock-role-timeout"
        vault_id = await _make_vault(pool, vault)
        git.init_vault(vault)
        doc = await service.put(DocumentPutRequest(
            vault=vault, collection="notes", title="First", content="first body",
        ))
        head = git.current_commit(vault)
        database = await pool.fetchval("SELECT current_database()")
        await pool.execute(f'ALTER ROLE CURRENT_USER IN DATABASE "{database}" SET lock_timeout = \'1s\'')
        # New sessions take the role's setting.
        await pool.expire_connections()

        async with pool.acquire() as holder, _OpenTransaction(holder):
            await holder.execute(
                "SELECT 1 FROM chunks WHERE source_type = 'document' AND source_id = $1 FOR UPDATE",
                (await pool.fetchval("SELECT id FROM documents WHERE vault_id = $1", vault_id)),
            )
            with pytest.raises(asyncpg.LockNotAvailableError) as failure:
                await asyncio.wait_for(
                    service.update(vault, doc.path, DocumentUpdateRequest(
                        content="changed body", expected_commit=head,
                    )),
                    timeout=_EVENT_LOCK_WAIT_SECONDS * 3,
                )
        assert failure.value.constraint_name is None
        assert transient_error(failure.value) is None
        # The lock wait came after the Git commit.
        assert git.current_commit(vault) != head


async def _blocked_backends(observer) -> set[int]:
    """Sessions of this database that wait for a lock another session holds."""
    rows = await observer.fetch(
        "SELECT pid FROM pg_stat_activity "
        "WHERE datname = current_database() AND cardinality(pg_blocking_pids(pid)) > 0",
    )
    return {row["pid"] for row in rows}


async def _until_blocked(observer, count: int) -> None:
    while len(await _blocked_backends(observer)) < count:
        await asyncio.sleep(0.01)


async def test_a_metadata_update_and_a_collection_delete_lock_in_one_order(tmp_path):
    """A recursive collection delete locks the collection and then the
    documents in it. An update that held its document while it asked for the
    collection deadlocked with it. Here the delete queues for the collection
    first and the update second; the update must not hold the document while
    it waits."""
    git = GitService(storage_path=str(tmp_path))
    documents = DocumentService(git=git)
    collections = CollectionService(git=git)
    async with _fresh_database() as pool:
        vault = "git-lock-collection-delete"
        vault_id = await _make_vault(pool, vault)
        git.init_vault(vault)
        doc = await documents.put(DocumentPutRequest(
            vault=vault, collection="notes", title="First", content="first body",
        ))
        collection_id = await pool.fetchval(
            "SELECT id FROM collections WHERE vault_id = $1 AND path = 'notes'", vault_id,
        )
        async with pool.acquire() as gate, pool.acquire() as observer:
            async with _OpenTransaction(gate):
                await gate.execute("SELECT 1 FROM collections WHERE id = $1 FOR UPDATE", collection_id)
                deleting = asyncio.create_task(collections.delete(
                    vault=vault, path="notes", recursive=True, agent_id="tester",
                ))
                await asyncio.wait_for(_until_blocked(observer, 1), timeout=10)
                updating = asyncio.create_task(documents.update(
                    vault, doc.path, DocumentUpdateRequest(title="Renamed"),
                ))
                await asyncio.wait_for(_until_blocked(observer, 2), timeout=10)
            # The gate is gone; the delete gets the collection first.
            deleted, updated = await asyncio.wait_for(
                asyncio.gather(deleting, updating, return_exceptions=True),
                timeout=_EVENT_LOCK_WAIT_SECONDS * 4,
            )
        assert not isinstance(deleted, BaseException), repr(deleted)
        # The collection delete removed the document before the update could lock it.
        assert isinstance(updated, NotFoundError), repr(updated)
        assert await pool.fetchval("SELECT count(*) FROM documents WHERE vault_id = $1", vault_id) == 0
        assert git.read_file(vault, doc.path) is None

