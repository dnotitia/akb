"""A recursive collection delete on the Native authority, document by document.

Each Native document's delete and its `document.delete` event commit together
in a short transaction that locks only that document's Resource. The
collections, files and tables, with `collection.delete`, go in a last
transaction once no live document is left under the prefix. A failure part-way
leaves the documents deleted so far deleted, each with its event, and the same
delete run again continues with the rest.

A database cut over from Bare Git keeps its `documents` rows as immutable
history. The delete used to try to delete them, which the cutover's fence
refuses, so it failed every time; it now leaves them, and the collections they
reference, in place.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import asyncpg
import pytest
from git import Repo

from app.db.transient import transient_error
from app.exceptions import ForbiddenError, NotFoundError
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.services import collection_service as collection_service_module
from app.services import document_counters, kg_service, native_derived_worker, queue_rescuer
from app.services.collection_service import CollectionService
from app.services.git_service import GitService
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_backfill import NativeRevisionBackfill
from app.services.native_revision_cutover import CutoverVaultInput, NativeRevisionCutover
from app.services._backfill import MAX_RETRIES
from app.repositories import table_data_repo
from app.services.native_derived_worker import NativeDerivedWorker
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_service import NativeRevisionService
from app.services.uri_service import doc_uri
from app.util.errors import PERMISSION_DENIED, WRITE_BUSY, exception_envelope
from tests.test_events_commit_order_postgres import (
    _EVENT_LOCK_WAIT_SECONDS,
    _OpenTransaction,
    _fresh_database,
    _make_vault,
)
from tests.test_native_revision_cutover_pg import _FixtureVerifier, _identity

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


async def _deletes_committed_with_their_events(pool, vault_id: uuid.UUID) -> bool:
    """Every document.delete event commits in its document's delete transaction."""
    rows = await pool.fetch(
        "SELECT e.xmin::text AS event_tx, r.xmin::text AS revision_tx "
        "FROM events e JOIN native_revisions r "
        "  ON r.resource_id = (e.payload->>'resource_id')::uuid AND r.action = 'delete' "
        "WHERE e.vault_id = $1 AND e.kind = 'document.delete'",
        vault_id,
    )
    return bool(rows) and all(r["event_tx"] == r["revision_tx"] for r in rows)


async def test_a_failure_after_the_first_document_is_completed_by_a_retry(monkeypatch):
    """Once the delete has removed its first document, another transaction
    holds the Vault's event lock past the deadline. The second document's
    delete rolls back with its event and the request fails with `write_busy`;
    the first stays deleted, with its event. Run again after the lock is free,
    the delete removes the second document and the collection."""
    async with _fresh_database() as pool:
        vault = "native-converge-busy"
        vault_id = await _native_vault(pool, monkeypatch, vault)

        delete_document = collection_service_module._delete_native_document
        held = asyncio.Event()

        async with pool.acquire() as holder:
            holding = _OpenTransaction(holder)
            await holding.__aenter__()
            try:
                async def busy_after_the_first(*args, **kwargs):
                    deleted = await delete_document(*args, **kwargs)
                    if not held.is_set():
                        # Take the Vault's event lock and keep it.
                        await emit_event(holder, "document.put", vault_id=vault_id, payload={})
                        held.set()
                    return deleted

                monkeypatch.setattr(collection_service_module, "_delete_native_document", busy_after_the_first)
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
            monkeypatch.setattr(collection_service_module, "_delete_native_document", delete_document)

        mapped = transient_error(failure.value)
        assert mapped is not None and (mapped.status_code, mapped.code) == (429, WRITE_BUSY)
        assert waited >= _EVENT_LOCK_WAIT_SECONDS - 0.5
        assert await _state(pool, vault_id) == {
            "documents": [("notes/a.md", "deleted"), ("notes/b.md", "live")],
            "collections": ["notes"],
            "events": ["document.delete"],
        }
        assert await _deletes_committed_with_their_events(pool, vault_id)

        result = await CollectionService(git=object()).delete(
            vault=vault, path="notes", recursive=True, agent_id="tester",
        )
        assert result["deleted_docs"] == 1
        assert await _state(pool, vault_id) == {
            "documents": [(path, "deleted") for path in _PATHS],
            "collections": [],
            "events": ["document.delete", "document.delete", "collection.delete"],
        }
        assert await _deletes_committed_with_their_events(pool, vault_id)


async def test_the_delete_holds_one_document_at_a_time(monkeypatch):
    """After the first document went, its Resource is free again: the delete
    holds no document while it works on the next, so it never waits for a
    second Resource while it holds one."""
    async with _fresh_database() as pool:
        vault = "native-converge-locks"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        first = await pool.fetchval(
            "SELECT resource_id FROM native_resources WHERE namespace_id = $1 AND current_path = $2",
            vault_id, _PATHS[0],
        )
        delete_document = collection_service_module._delete_native_document
        observed: list[bool] = []

        async def probe_after_the_first(*args, **kwargs):
            deleted = await delete_document(*args, **kwargs)
            if not observed:
                async with pool.acquire() as probe:
                    async with probe.transaction():
                        free = await probe.fetchval(
                            "SELECT resource_id FROM native_resources WHERE resource_id = $1 "
                            "FOR UPDATE NOWAIT", first,
                        )
                observed.append(free == first)
            return deleted

        monkeypatch.setattr(collection_service_module, "_delete_native_document", probe_after_the_first)
        result = await asyncio.wait_for(
            CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester",
            ),
            timeout=_EVENT_LOCK_WAIT_SECONDS * 3,
        )
        assert result["deleted_docs"] == 2
        assert observed == [True]
        rows = await pool.fetch(
            "SELECT kind, xmin::text AS tx FROM events WHERE vault_id = $1 "
            "AND kind IN ('document.delete', 'collection.delete') ORDER BY id",
            vault_id,
        )
        assert [r["kind"] for r in rows] == ["document.delete", "document.delete", "collection.delete"]
        # Each document in a transaction of its own, the collections in a last one.
        assert len({r["tx"] for r in rows}) == 3
        assert await _deletes_committed_with_their_events(pool, vault_id)


async def _hold_another_request(pool, failpoint_boundary: str):
    """A Native service whose mutations stop before they commit."""
    before_commit = asyncio.Event()
    release_commit = asyncio.Event()

    async def hold(boundary: str) -> None:
        if boundary == failpoint_boundary:
            before_commit.set()
            await release_commit.wait()

    service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool), failpoint=hold)
    return service, before_commit, release_commit


async def _held_session(observer) -> int:
    pid = await observer.fetchval(
        "SELECT pid FROM pg_stat_activity "
        "WHERE datname = current_database() AND state = 'idle in transaction'",
    )
    assert pid is not None
    return pid


@pytest.mark.parametrize("change", ["deleted", "moved out"])
async def test_a_document_changed_while_the_delete_waits_is_left_and_not_counted(monkeypatch, change):
    """Another request deletes `notes/a.md`, or moves it out of `notes`, and
    has not committed when the recursive delete asks for its Resource. Once it
    commits, the delete leaves the document as that request left it, deletes
    the rest and counts only what it deleted."""
    async with _fresh_database() as pool:
        vault = "native-converge-" + change.replace(" ", "-")
        vault_id = await _native_vault(pool, monkeypatch, vault)
        first = await pool.fetchrow(
            "SELECT resource_id, head_revision_id FROM native_resources "
            "WHERE namespace_id = $1 AND current_path = $2",
            vault_id, _PATHS[0],
        )
        other, before_commit, release_commit = await _hold_another_request(pool, "authority.before_commit")
        if change == "deleted":
            changing = asyncio.create_task(other.delete_resource(
                namespace_id=vault_id, surface="document", path=_PATHS[0],
                actor="other", mutation_id=uuid.uuid4(),
                expected_revision_id=first["head_revision_id"],
                expected_resource_id=first["resource_id"],
            ))
        else:
            changing = asyncio.create_task(other.move_text(
                namespace_id=vault_id, surface="document", path=_PATHS[0], path_to="elsewhere/a.md",
                actor="other", mutation_id=uuid.uuid4(),
                expected_revision_id=first["head_revision_id"],
                expected_resource_id=first["resource_id"],
            ))
        deleting: asyncio.Task | None = None
        async with pool.acquire() as observer:
            try:
                await asyncio.wait_for(before_commit.wait(), timeout=10)
                other_pid = await _held_session(observer)
                deleting = asyncio.create_task(CollectionService(git=object()).delete(
                    vault=vault, path="notes", recursive=True, agent_id="tester",
                ))

                async def delete_waits_for_the_resource() -> None:
                    while not await observer.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))",
                        other_pid,
                    ):
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(delete_waits_for_the_resource(), timeout=10)
                release_commit.set()
                await asyncio.wait_for(changing, timeout=10)
                result = await asyncio.wait_for(deleting, timeout=_EVENT_LOCK_WAIT_SECONDS * 3)
            finally:
                release_commit.set()
                for task in (changing, deleting):
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task

        assert result["deleted_docs"] == 1
        state = await _state(pool, vault_id)
        if change == "deleted":
            assert state["documents"] == [(path, "deleted") for path in _PATHS]
        else:
            assert state["documents"] == [("elsewhere/a.md", "live"), ("notes/b.md", "deleted")]
        assert "notes" not in state["collections"]


async def _cut_over_with_a_collection(pool, git: GitService) -> tuple[str, uuid.UUID, uuid.UUID]:
    """A Bare Git Vault with `p/a.md`, cut over to the Native authority."""
    vault = f"cutover-collection-{uuid.uuid4().hex[:8]}"
    git.init_vault(vault)
    oid = git.commit_file(
        vault, "p/a.md", "fixture body\n",
        "[create] p/a.md\n\nagent: fixture\naction: create\nsummary: a",
    )
    committed_at = Repo(str(git._bare_path(vault))).commit(oid).committed_datetime
    vault_id = await pool.fetchval(
        "INSERT INTO vaults (name, git_path, status) VALUES ($1, $2, 'active') RETURNING id",
        vault, str(git._bare_path(vault)),
    )
    collection_id = await CollectionRepository(pool).get_or_create(vault_id, "p")
    doc_id = uuid.uuid4()
    await pool.execute(
        "INSERT INTO documents (id, vault_id, collection_id, path, title, created_at, updated_at, "
        "current_commit, source) VALUES ($1, $2, $3, 'p/a.md', 'A', $4, $4, $5, 'manual')",
        doc_id, vault_id, collection_id, committed_at, oid,
    )
    cutover = NativeRevisionCutover(
        pool, backfill=NativeRevisionBackfill(pool, git=git), verifier=_FixtureVerifier(pool),
    )
    planned = await cutover.plan(
        vaults=[CutoverVaultInput(namespace_id=vault_id, fixed_ref=oid)],
        coverage_version="fixture-collection-v1",
    )
    await cutover.apply(planned.cutover_id)
    await cutover.verify(planned.cutover_id)
    await cutover.commit(planned.cutover_id, identity=_identity("collection"))
    return vault, vault_id, doc_id


async def test_a_recursive_delete_after_a_cutover_leaves_the_preserved_rows(tmp_path, monkeypatch):
    """`p/a.md` was cut over: its `documents` row stays, fenced, and the
    Native Resource of the same id is the authority. `p/new` holds a document
    written after the cutover. The delete removes both documents and `p/new`;
    `p` keeps its row, which the preserved row references, and the delete run
    again succeeds."""
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: True)
    async with _fresh_database() as pool:
        git = GitService(storage_path=str(tmp_path / "git"))
        vault, vault_id, doc_id = await _cut_over_with_a_collection(pool, git)
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        await native.create_text(
            namespace_id=vault_id, surface="document", path="p/new/b.md",
            payload="after the cutover", actor="tester", mutation_id=uuid.uuid4(),
        )
        await CollectionRepository(pool).get_or_create(vault_id, "p/new")
        preserved = dict(await pool.fetchrow("SELECT * FROM documents WHERE id = $1", doc_id))

        result = await CollectionService(git=object()).delete(
            vault=vault, path="p", recursive=True, agent_id="tester",
        )
        assert (result["deleted_docs"], result["deleted_sub_collections"]) == (2, 1)
        # The response says which collection stayed, and why.
        assert result["retained_collections"] == ["p"]
        assert result["hint"] == collection_service_module.RETAINED_COLLECTIONS_HINT
        assert await pool.fetchval(
            "SELECT count(*) FROM native_resources WHERE namespace_id = $1 AND lifecycle = 'live'",
            vault_id,
        ) == 0
        assert dict(await pool.fetchrow("SELECT * FROM documents WHERE id = $1", doc_id)) == preserved
        assert [r["path"] for r in await pool.fetch(
            "SELECT path FROM collections WHERE vault_id = $1", vault_id,
        )] == ["p"]
        payload = await pool.fetchval(
            "SELECT payload FROM events WHERE vault_id = $1 AND kind = 'collection.delete'", vault_id,
        )
        assert '"retained_collections": ["p"]' in payload

        again = await CollectionService(git=object()).delete(
            vault=vault, path="p", recursive=True, agent_id="tester",
        )
        assert (again["deleted_docs"], again["deleted_sub_collections"]) == (0, 0)
        assert (again["retained_collections"], again["hint"]) == (["p"], result["hint"])
        # Browse shows the retained collection, empty, as the response says.
        browsed = await NativeDocumentService(pool=pool).browse(vault)
        assert [(item.path, item.type) for item in browsed.items] == [("p", "collection")]


async def _link(vault: str, source: str, target: str) -> None:
    result = await kg_service.link_resources(
        vault, doc_uri(vault, source), doc_uri(vault, target), "related_to", created_by="tester",
    )
    assert not result.get("error"), result


async def _link_count(pool, vault_id: uuid.UUID, vault: str, source: str, target: str) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM edges WHERE vault_id = $1 AND source_uri = $2 AND target_uri = $3",
        vault_id, doc_uri(vault, source), doc_uri(vault, target),
    )


async def _drain(worker: NativeDerivedWorker, rounds: int = 20) -> None:
    for _ in range(rounds):
        await worker.process_once()


async def test_a_document_restored_after_its_delete_has_only_its_new_links(monkeypatch):
    """`notes/a.md` links to `other/c.md`. The recursive delete commits
    `notes/a.md`'s delete and is held right after that commit, before the next
    document. Another request then restores the same Resource and moves it to
    `elsewhere/a.md`: the link it had before the delete is gone, because the
    delete removed it in its own transaction. A link made after the restore
    stays, whatever the delete and the derived worker do afterwards. The
    delete used to leave the old link to the worker, which skips an intent
    the Resource has moved past, so a restored document got it back."""
    async with _fresh_database() as pool:
        vault = "native-restored-links"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        await native.create_text(
            namespace_id=vault_id, surface="document", path="other/c.md",
            payload="body of other/c.md", actor="tester", mutation_id=uuid.uuid4(),
        )
        await _link(vault, _PATHS[0], "other/c.md")
        first = await pool.fetchval(
            "SELECT resource_id FROM native_resources WHERE namespace_id = $1 AND current_path = $2",
            vault_id, _PATHS[0],
        )
        committed, resume = asyncio.Event(), asyncio.Event()
        delete_document = collection_service_module._delete_native_document

        async def hold_after_the_first(*args, **kwargs):
            deleted = await delete_document(*args, **kwargs)
            if kwargs.get("resource_id") == first:
                committed.set()
                await resume.wait()
            return deleted

        monkeypatch.setattr(collection_service_module, "_delete_native_document", hold_after_the_first)
        deleting = asyncio.create_task(CollectionService(git=object()).delete(
            vault=vault, path="notes", recursive=True, agent_id="tester",
        ))
        try:
            await asyncio.wait_for(committed.wait(), timeout=30)
            # The delete is held between its documents.
            assert not deleting.done()
            deleted = await pool.fetchval(
                "SELECT head_revision_id FROM native_resources WHERE resource_id = $1 AND lifecycle = 'deleted'",
                first,
            )
            assert deleted is not None
            restored = await native.restore_text(
                namespace_id=vault_id, surface="document", path=_PATHS[0], payload="restored body",
                actor="other", mutation_id=uuid.uuid4(), expected_revision_id=deleted,
                expected_resource_id=first,
            )
            await native.move_text(
                namespace_id=vault_id, surface="document", path=_PATHS[0], path_to="elsewhere/a.md",
                actor="other", mutation_id=uuid.uuid4(), expected_revision_id=restored.revision_id,
                expected_resource_id=first,
            )
            assert await pool.fetchval(
                "SELECT count(*) FROM edges WHERE vault_id = $1 AND (source_resource_id = $2 OR target_resource_id = $2)",
                vault_id, first,
            ) == 0
            await _link(vault, "elsewhere/a.md", "other/c.md")
        finally:
            resume.set()
        await asyncio.wait_for(deleting, timeout=30)
        await _drain(NativeDerivedWorker(pool))

        assert await _link_count(pool, vault_id, vault, "elsewhere/a.md", "other/c.md") == 1
        assert await _link_count(pool, vault_id, vault, _PATHS[0], "other/c.md") == 0
        assert await pool.fetchval(
            "SELECT lifecycle FROM native_resources WHERE resource_id = $1", first,
        ) == "live"


async def test_a_delete_stopped_after_its_documents_is_completed_by_a_retry(monkeypatch):
    """The documents under `notes`, which has no collection row, were deleted
    with their events, and the delete failed before its last transaction, so
    nothing is left under `notes`. Run again, the delete succeeds with nothing
    to delete and records the `collection.delete` the first run did not. The
    event is at least once: a delete run once more records it again. It used
    to report `notes` as not found. A prefix nothing was deleted under is still
    not found."""
    async with _fresh_database() as pool:
        vault = "native-converge-final"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        await pool.execute("DELETE FROM collections WHERE vault_id = $1", vault_id)
        last_transaction = CollectionService._delete_collections
        failed: list[bool] = []

        async def fail_once(self, **kwargs):
            if not failed:
                failed.append(True)
                raise RuntimeError("the delete stopped before its last transaction")
            return await last_transaction(self, **kwargs)

        monkeypatch.setattr(CollectionService, "_delete_collections", fail_once)

        def delete(path: str = "notes"):
            return CollectionService(git=object()).delete(
                vault=vault, path=path, recursive=True, agent_id="tester",
            )

        with pytest.raises(RuntimeError):
            await delete()
        assert await _state(pool, vault_id) == {
            "documents": [(path, "deleted") for path in _PATHS],
            "collections": [],
            "events": ["document.delete", "document.delete"],
        }

        nothing = {"deleted_docs": 0, "deleted_files": 0, "deleted_sub_collections": 0, "deleted_tables": 0}
        assert await delete() == {"ok": True, "collection": "notes", **nothing}
        completed = ["document.delete", "document.delete", "collection.delete"]
        assert (await _state(pool, vault_id))["events"] == completed
        assert await delete() == {"ok": True, "collection": "notes", **nothing}
        assert (await _state(pool, vault_id))["events"] == [*completed, "collection.delete"]
        with pytest.raises(NotFoundError):
            await delete("never")


async def test_the_worker_retries_a_cleanup_that_deadlocked(monkeypatch):
    """The derived worker removes a deleted document's chunks, and its graph
    edges once more, in the transaction that applies the delete's intent.
    Edges are rows other writers lock in their own order, so that transaction
    can end in a deadlock (40P01). The intent is then scheduled again with the
    worker's backoff, and its next attempt applies it."""
    async with _fresh_database() as pool:
        vault = "native-cleanup-retry"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        first = await pool.fetchval(
            "SELECT resource_id FROM native_resources WHERE namespace_id = $1 AND current_path = $2",
            vault_id, _PATHS[0],
        )
        await CollectionService(git=object()).delete(
            vault=vault, path="notes", recursive=True, agent_id="tester",
        )
        cleanup = native_derived_worker.delete_native_document_edges
        deadlocks: list[uuid.UUID] = []

        async def deadlock_once(conn, vault_id, vault_name, resource_id, path):
            if resource_id == first and not deadlocks:
                deadlocks.append(resource_id)
                raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
            return await cleanup(conn, vault_id, vault_name, resource_id, path)

        monkeypatch.setattr(native_derived_worker, "delete_native_document_edges", deadlock_once)
        worker = NativeDerivedWorker(pool)
        await _drain(worker)
        assert deadlocks == [first]
        assert await _open_intents(pool, first) == [(1, "DeadlockDetectedError")]
        later = await pool.fetchval(
            "SELECT next_attempt_at > NOW() + INTERVAL '50 seconds' FROM native_invalidation_intents "
            "WHERE resource_id = $1 AND completed_at IS NULL",
            first,
        )
        assert later, "the next attempt waits for the first backoff step"

        await _attempt_now(pool, first)
        await _drain(worker)
        assert await _open_intents(pool, first) == []
        assert await _last_outcome(pool, first) == "deleted"


async def _open_intents(pool, resource_id: uuid.UUID) -> list[tuple[int, str | None]]:
    rows = await pool.fetch(
        "SELECT retry_count, last_error FROM native_invalidation_intents "
        "WHERE resource_id = $1 AND completed_at IS NULL",
        resource_id,
    )
    return [(r["retry_count"], r["last_error"]) for r in rows]


async def _attempt_now(pool, resource_id: uuid.UUID) -> None:
    await pool.execute(
        "UPDATE native_invalidation_intents SET next_attempt_at = NOW() "
        "WHERE resource_id = $1 AND completed_at IS NULL",
        resource_id,
    )


async def _last_outcome(pool, resource_id: uuid.UUID) -> str | None:
    return await pool.fetchval(
        "SELECT delivery_outcome FROM native_invalidation_intents WHERE resource_id = $1 "
        "ORDER BY occurred_at DESC, intent_id DESC LIMIT 1",
        resource_id,
    )


async def _derived(pool, resource_id: uuid.UUID) -> dict:
    """The derived state of a Native document: its chunks, derived head and
    the vector deletes queued for its chunks."""
    chunk_ids = [r["id"] for r in await pool.fetch(
        "SELECT id FROM chunks WHERE source_type = 'native_document' AND source_id = $1", resource_id,
    )]
    return {
        "chunks": chunk_ids,
        "head": await pool.fetchval(
            "SELECT revision_id FROM native_derived_heads WHERE resource_id = $1", resource_id,
        ),
        "vector_deletes": [r["chunk_id"] for r in await pool.fetch(
            "SELECT chunk_id FROM vector_delete_outbox WHERE source_type = 'native_document' "
            "AND source_id = $1 ORDER BY chunk_id",
            resource_id,
        )],
    }


async def _indexed_then_deleted(pool, monkeypatch, vault: str) -> tuple[uuid.UUID, uuid.UUID, dict]:
    """A Native Vault whose documents the worker indexed, then deleted
    recursively; returns the Vault, its first document and that document's
    derived state before the delete."""
    vault_id = await _native_vault(pool, monkeypatch, vault)
    first = await pool.fetchval(
        "SELECT resource_id FROM native_resources WHERE namespace_id = $1 AND current_path = $2",
        vault_id, _PATHS[0],
    )
    await _drain(NativeDerivedWorker(pool))
    indexed = await _derived(pool, first)
    assert indexed["chunks"] and indexed["head"] is not None and indexed["vector_deletes"] == []
    await CollectionService(git=object()).delete(
        vault=vault, path="notes", recursive=True, agent_id="tester",
    )
    return vault_id, first, indexed


async def test_a_cleanup_that_keeps_failing_is_abandoned_and_can_be_requeued(monkeypatch):
    """The documents were indexed, with chunks and a derived head, before the
    recursive delete. Applying the delete's intent fails on every attempt, and
    each failure rolls back, so the chunks stay. After its `MAX_RETRIES`-th
    attempt the intent is abandoned: completed with outcome `abandoned` and
    counted in the queue's statistics. An operator re-opens it with
    `requeue_abandoned`; once the cause is gone, the worker removes the
    chunks and the derived head and queues the vector deletes of exactly
    those chunks."""
    async with _fresh_database() as pool:
        vault_id, first, indexed = await _indexed_then_deleted(pool, monkeypatch, "native-cleanup-abandoned")
        cleanup = native_derived_worker.delete_native_document_edges
        broken = {"on": True}

        async def always_deadlocks(conn, vault_id, vault_name, resource_id, path):
            if resource_id == first and broken["on"]:
                raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
            return await cleanup(conn, vault_id, vault_name, resource_id, path)

        monkeypatch.setattr(native_derived_worker, "delete_native_document_edges", always_deadlocks)
        worker = NativeDerivedWorker(pool)
        for attempt in range(1, MAX_RETRIES):
            await _drain(worker)
            assert await _open_intents(pool, first) == [(attempt, "DeadlockDetectedError")]
            await _attempt_now(pool, first)
        await _drain(worker)
        assert await _open_intents(pool, first) == []
        assert await _last_outcome(pool, first) == "abandoned"
        assert (await worker.pending_stats(vault_id))["abandoned"] >= 1
        assert await _derived(pool, first) == indexed

        broken["on"] = False
        assert await worker.requeue_abandoned(namespace_id=vault_id, resource_id=first) == 1
        assert await worker.requeue_abandoned(namespace_id=vault_id, resource_id=first) == 0
        await _drain(worker)
        assert await _last_outcome(pool, first) == "deleted"
        assert await _derived(pool, first) == {
            "chunks": [], "head": None, "vector_deletes": sorted(indexed["chunks"]),
        }


async def test_a_final_claim_that_expires_is_abandoned_by_the_rescuer(monkeypatch):
    """A claim that ends without an outcome, as when its process stops, is
    claimed again once its 10-minute lease expires, except the final one: an
    intent claimed for its `MAX_RETRIES`-th attempt is not claimed again, and
    the queue rescuer marks it abandoned once its lease expired. Its
    `last_error` stays the exception class of the last failed attempt, or is
    `claim lease expired after final attempt` when no attempt recorded one.
    Re-opened, it is applied."""
    async with _fresh_database() as pool:
        vault_id, first, indexed = await _indexed_then_deleted(pool, monkeypatch, "native-final-claim")
        second = await pool.fetchval(
            "SELECT resource_id FROM native_resources WHERE namespace_id = $1 AND current_path = $2",
            vault_id, _PATHS[1],
        )
        # Seven attempts ended without an outcome; the second document's last
        # one recorded an exception.
        await pool.execute(
            "UPDATE native_invalidation_intents SET retry_count = $1 "
            "WHERE namespace_id = $2 AND completed_at IS NULL",
            MAX_RETRIES - 1, vault_id,
        )
        await pool.execute(
            "UPDATE native_invalidation_intents SET last_error = 'DeadlockDetectedError' "
            "WHERE resource_id = $1 AND completed_at IS NULL",
            second,
        )
        worker = NativeDerivedWorker(pool)
        # The final claims, whose process stops before an outcome.
        for _ in range(2):
            assert (await worker._claim_one())["retry_count"] == MAX_RETRIES
        assert await worker._claim_one() is None
        await pool.execute(
            "UPDATE native_invalidation_intents SET next_attempt_at = NOW() - INTERVAL '1 second' "
            "WHERE namespace_id = $1 AND completed_at IS NULL",
            vault_id,
        )
        assert await worker.process_once() == 0
        assert await queue_rescuer._rescue_once() == 2
        outcomes = {
            r["resource_id"]: (r["delivery_outcome"], r["last_error"])
            for r in await pool.fetch(
                "SELECT resource_id, delivery_outcome, last_error FROM native_invalidation_intents "
                "WHERE namespace_id = $1 AND completed_at IS NOT NULL AND delivery_outcome = 'abandoned'",
                vault_id,
            )
        }
        assert outcomes == {
            first: ("abandoned", "claim lease expired after final attempt"),
            second: ("abandoned", "DeadlockDetectedError"),
        }
        assert await _derived(pool, first) == indexed

        assert await worker.requeue_abandoned(namespace_id=vault_id) == 2
        await _drain(worker)
        assert await _last_outcome(pool, first) == "deleted"
        assert (await _derived(pool, first))["chunks"] == []


async def test_requeue_reopens_only_an_abandoned_intent_at_the_head(monkeypatch):
    """`notes/a.md`'s first Revision R1 was abandoned, and its current Revision
    R2 waits to be indexed. R1's intent carries the later `occurred_at`, as an
    intent written by a process whose clock runs ahead can. The worker keeps
    one pending intent per Resource, the one stamped last. Re-opening R1 made
    it close R2's intent as superseded and then close R1 too, since R1 is not
    the Head: R2 was never indexed, while the queue looked settled. Now R1 is
    closed as superseded instead of re-opened, and R2 is indexed."""
    async with _fresh_database() as pool:
        vault_id = await _native_vault(pool, monkeypatch, "native-requeue-head")
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        first = await pool.fetchrow(
            "SELECT resource_id, head_revision_id FROM native_resources "
            "WHERE namespace_id = $1 AND current_path = $2",
            vault_id, _PATHS[0],
        )
        await pool.execute(
            "UPDATE native_invalidation_intents SET completed_at = NOW(), delivery_outcome = 'abandoned', "
            "retry_count = $2, last_error = 'DeadlockDetectedError' WHERE revision_id = $1",
            first["head_revision_id"], MAX_RETRIES,
        )
        current = await native.replace_text(
            namespace_id=vault_id, surface="document", path=_PATHS[0], payload="current body token",
            actor="tester", mutation_id=uuid.uuid4(), expected_revision_id=first["head_revision_id"],
            expected_resource_id=first["resource_id"],
        )
        await pool.execute(
            "UPDATE native_invalidation_intents SET occurred_at = ("
            "  SELECT occurred_at FROM native_invalidation_intents WHERE revision_id = $2"
            ") + INTERVAL '1 second' WHERE revision_id = $1",
            first["head_revision_id"], current.revision_id,
        )
        worker = NativeDerivedWorker(pool)
        assert await worker.requeue_abandoned(namespace_id=vault_id, resource_id=first["resource_id"]) == 0
        await _drain(worker)
        outcomes = {
            r["revision_id"]: r["delivery_outcome"]
            for r in await pool.fetch(
                "SELECT revision_id, delivery_outcome FROM native_invalidation_intents WHERE resource_id = $1",
                first["resource_id"],
            )
        }
        assert outcomes == {first["head_revision_id"]: "superseded", current.revision_id: "applied"}
        derived = await _derived(pool, first["resource_id"])
        assert derived["head"] == current.revision_id
        assert await pool.fetchval(
            "SELECT bool_and(content LIKE '%current body token%') FROM chunks "
            "WHERE source_type = 'native_document' AND source_id = $1",
            first["resource_id"],
        )
        stats = await worker.pending_stats(vault_id)
        assert (stats["pending"], stats["abandoned_at_head"], stats["status"]) == (0, 0, "ok")


async def test_a_table_created_after_the_check_refuses_the_rest_and_lists_what_went(monkeypatch):
    """A writer may not delete a collection holding a table. Its recursive
    delete of `notes` found no table, and then another request created one
    under `notes` while the documents were deleted. The last transaction
    finds the table and refuses, after both documents went: the refusal
    says the delete partly completed and lists the deleted documents, over
    MCP and REST alike. The same writer is refused again before anything
    else goes; an admin's delete finishes it, the table included."""
    async with _fresh_database() as pool:
        vault = "native-table-appears"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        check = CollectionService._refuse_tables_under
        created: list[str] = []

        async def table_created_after_the_check(self, vault_name, vid, norm):
            await check(self, vault_name, vid, norm)
            if not created:
                pg_name = table_data_repo.pg_table_name(vault, "late")
                columns = [{"name": "value", "type": "text"}]
                async with pool.acquire() as conn:
                    await table_data_repo.create_dynamic_table(
                        conn, pg_name, columns, vault_name=vault, vault_id=vault_id,
                        resource_uri=f"akb://{vault}/table/late",
                    )
                    await conn.execute(
                        "INSERT INTO vault_tables (id, vault_id, name, description, columns, unique_keys, "
                        "indexes, collection_id) VALUES ($1, $2, 'late', '', $3::jsonb, '[]'::jsonb, "
                        "'[]'::jsonb, (SELECT id FROM collections WHERE vault_id = $2 AND path = 'notes'))",
                        uuid.uuid4(), vault_id, '[{"name": "value", "type": "text"}]',
                    )
                created.append(pg_name)

        monkeypatch.setattr(CollectionService, "_refuse_tables_under", table_created_after_the_check)

        def delete(*, admin: bool):
            return CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", allow_table_delete=admin,
            )

        with pytest.raises(ForbiddenError) as partial:
            await delete(admin=False)
        uris = [doc_uri(vault, path) for path in _PATHS]
        assert partial.value.details == {"partially_applied": True, "deleted_docs": 2, "deleted_uris": uris}
        assert "'admin' role" in partial.value.hint
        envelope = exception_envelope(partial.value)
        assert (envelope["code"], envelope["details"]["deleted_uris"]) == (PERMISSION_DENIED, uris)
        assert envelope["hint"] == partial.value.hint
        assert await _state(pool, vault_id) == {
            "documents": [(path, "deleted") for path in _PATHS],
            "collections": ["notes"],
            "events": ["document.delete", "document.delete"],
        }

        with pytest.raises(ForbiddenError) as again:
            await delete(admin=False)
        assert again.value.details is None

        result = await delete(admin=True)
        assert (result["deleted_docs"], result["deleted_tables"]) == (0, 1)
        assert (await _state(pool, vault_id))["collections"] == []
        assert await pool.fetchval("SELECT to_regclass($1)", created[0]) is None
