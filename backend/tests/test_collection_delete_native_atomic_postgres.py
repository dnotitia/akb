"""A recursive collection delete on the Native authority deletes its
documents and its collections in one transaction.

It used to delete each Native document in a transaction of its own, which
committed the document's delete and its event before the next document was
read. A failure after the first document, such as the Vault's event lock
staying busy past its deadline (`write_busy`), rolled back the collections and
left the documents deleted so far deleted. The documents are now deleted on
the delete's own connection, with every Resource row and path locked before
the first of them is deleted, and their events are inserted with the
collection's at the end of the transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import asyncpg
import pytest

from app.db.transient import transient_error
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.services import collection_service as collection_service_module
from app.services import document_counters
from app.services.collection_service import CollectionService
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from app.util.errors import WRITE_BUSY
from tests.test_events_commit_order_postgres import (
    _EVENT_LOCK_WAIT_SECONDS,
    _OpenTransaction,
    _fresh_database,
    _make_vault,
)

_PATHS = ("notes/a.md", "notes/b.md")


async def _native_vault(pool, monkeypatch, name: str) -> uuid.UUID:
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: True)
    vault_id = await _make_vault(pool, name)
    native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
    for path in _PATHS:
        await native.create_text(
            namespace_id=vault_id, surface="document", path=path,
            payload=f"body of {path}", actor="tester", mutation_id=uuid.uuid4(),
        )
    await CollectionRepository(pool).get_or_create(vault_id, "notes")
    return vault_id


async def _state(pool, vault_id: uuid.UUID) -> dict:
    lifecycles = await pool.fetch(
        "SELECT current_path, lifecycle FROM native_resources "
        "WHERE namespace_id = $1 AND surface = 'document' ORDER BY current_path",
        vault_id,
    )
    return {
        "documents": [(r["current_path"], r["lifecycle"]) for r in lifecycles],
        "collections": [r["path"] for r in await pool.fetch(
            "SELECT path FROM collections WHERE vault_id = $1 ORDER BY path", vault_id,
        )],
        "events": [r["kind"] for r in await pool.fetch(
            "SELECT kind FROM events WHERE vault_id = $1 AND kind LIKE ANY(ARRAY['document.delete', "
            "'collection.delete']) ORDER BY id",
            vault_id,
        )],
    }


async def test_a_busy_event_lock_after_the_first_document_rolls_the_whole_delete_back(
    monkeypatch,
):
    """Once the delete has removed its first document, another transaction
    holds the Vault's event lock past the deadline. The delete fails with
    `write_busy` and nothing of it remains: both documents are live and the
    collection exists. Run again after the lock is free, it deletes both."""
    async with _fresh_database() as pool:
        vault = "native-atomic-busy"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        before = await _state(pool, vault_id)

        delete_document_chunks = collection_service_module.delete_document_chunks
        held = asyncio.Event()

        async with pool.acquire() as holder:
            holding = _OpenTransaction(holder)
            await holding.__aenter__()
            try:
                async def busy_after_the_first(conn, source_id):
                    if not held.is_set():
                        # Take the Vault's event lock and keep it.
                        await emit_event(holder, "document.put", vault_id=vault_id, payload={})
                        held.set()
                    return await delete_document_chunks(conn, source_id)

                monkeypatch.setattr(collection_service_module, "delete_document_chunks", busy_after_the_first)
                started = time.monotonic()
                with pytest.raises(asyncpg.LockNotAvailableError) as failure:
                    await asyncio.wait_for(
                        CollectionService(git=object()).delete(
                            vault=vault, path="notes", recursive=True, agent_id="tester",
                        ),
                        timeout=_EVENT_LOCK_WAIT_SECONDS * 3,
                    )
                waited = time.monotonic() - started
            finally:
                await holding.__aexit__(None, None, None)
            monkeypatch.setattr(collection_service_module, "delete_document_chunks", delete_document_chunks)

        assert held.is_set()
        mapped = transient_error(failure.value)
        assert mapped is not None and (mapped.status_code, mapped.code) == (429, WRITE_BUSY)
        assert waited >= _EVENT_LOCK_WAIT_SECONDS - 0.5
        # Nothing of the failed delete remains.
        assert await _state(pool, vault_id) == before

        result = await CollectionService(git=object()).delete(
            vault=vault, path="notes", recursive=True, agent_id="tester",
        )
        assert result["deleted_docs"] == 2
        assert await _state(pool, vault_id) == {
            "documents": [(path, "deleted") for path in _PATHS],
            "collections": [],
            "events": ["document.delete", "document.delete", "collection.delete"],
        }


async def test_the_delete_inserts_its_events_last_in_one_transaction(monkeypatch):
    """While the delete works on its documents it does not hold the Vault's
    event lock, so another writer of the Vault inserts an event without
    waiting; the delete's document and collection events commit together."""
    async with _fresh_database() as pool:
        vault = "native-atomic-events"
        vault_id = await _native_vault(pool, monkeypatch, vault)

        delete_document_chunks = collection_service_module.delete_document_chunks
        probe_waits: list[float] = []

        async def probe_after_the_first(conn, source_id):
            if not probe_waits:
                async with pool.acquire() as probe:
                    async with _OpenTransaction(probe):
                        started = time.monotonic()
                        await emit_event(probe, "document.put", vault_id=vault_id, payload={})
                        probe_waits.append(time.monotonic() - started)
            return await delete_document_chunks(conn, source_id)

        monkeypatch.setattr(collection_service_module, "delete_document_chunks", probe_after_the_first)
        result = await asyncio.wait_for(
            CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester",
            ),
            timeout=_EVENT_LOCK_WAIT_SECONDS * 3,
        )

        assert result["deleted_docs"] == 2
        assert probe_waits and probe_waits[0] < 1.0, probe_waits
        rows = await pool.fetch(
            "SELECT kind, payload->>'path' AS path, xmin::text AS tx FROM events "
            "WHERE vault_id = $1 ORDER BY id",
            vault_id,
        )
        deleting = [r for r in rows if r["kind"] in {"document.delete", "collection.delete"}]
        # The documents go in the order their rows were locked (by id).
        assert sorted((r["kind"], r["path"]) for r in deleting[:-1]) == [
            ("document.delete", path) for path in _PATHS
        ]
        assert (deleting[-1]["kind"], deleting[-1]["path"]) == ("collection.delete", "notes")
        assert len({r["tx"] for r in deleting}) == 1
        # They are the transaction's last statements: no other event follows.
        assert rows[-1]["kind"] == "collection.delete"


async def test_a_document_deleted_while_the_delete_waits_is_not_counted(monkeypatch):
    """Another request deletes a document and has not committed when the
    recursive delete asks for its row. Once that request commits, the
    recursive delete leaves the document as it is, deletes the rest and does
    not count it."""
    async with _fresh_database() as pool:
        vault = "native-atomic-gone"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        first = await pool.fetchrow(
            "SELECT resource_id, head_revision_id FROM native_resources "
            "WHERE namespace_id = $1 AND current_path = $2",
            vault_id, _PATHS[0],
        )
        before_commit = asyncio.Event()
        release_commit = asyncio.Event()

        async def hold_before_commit(boundary: str) -> None:
            if boundary == "authority.before_commit":
                before_commit.set()
                await release_commit.wait()

        other = NativeRevisionService(
            pool, payload_store=M1PgBodyStore(pool), failpoint=hold_before_commit,
        )
        other_delete = asyncio.create_task(other.delete_resource(
            namespace_id=vault_id, surface="document", path=_PATHS[0],
            actor="other", mutation_id=uuid.uuid4(),
            expected_revision_id=first["head_revision_id"],
            expected_resource_id=first["resource_id"],
        ))
        deleting: asyncio.Task | None = None
        async with pool.acquire() as observer:
            try:
                await asyncio.wait_for(before_commit.wait(), timeout=10)
                # The other delete's session, held open inside its transaction.
                other_pid = await observer.fetchval(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'idle in transaction'",
                )
                assert other_pid is not None
                deleting = asyncio.create_task(CollectionService(git=object()).delete(
                    vault=vault, path="notes", recursive=True, agent_id="tester",
                ))

                async def delete_waits_for_the_row() -> None:
                    while not await observer.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))",
                        other_pid,
                    ):
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(delete_waits_for_the_row(), timeout=10)
                release_commit.set()
                await asyncio.wait_for(other_delete, timeout=10)
                result = await asyncio.wait_for(deleting, timeout=_EVENT_LOCK_WAIT_SECONDS * 3)
            finally:
                release_commit.set()
                for task in (other_delete, deleting):
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task

        assert result["deleted_docs"] == 1
        state = await _state(pool, vault_id)
        assert state["documents"] == [(path, "deleted") for path in _PATHS]
        assert state["collections"] == []
        actors = [r["actor_id"] for r in await pool.fetch(
            "SELECT actor_id FROM events WHERE vault_id = $1 AND kind = 'document.delete' ORDER BY id",
            vault_id,
        )]
        assert actors == ["other", "tester"]
        assert state["events"] == ["document.delete", "document.delete", "collection.delete"]
