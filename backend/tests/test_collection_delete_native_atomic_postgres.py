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
import json
import time
import uuid

import asyncpg
import pytest
from git import Repo

from app.exceptions import (
    AKBError,
    ConflictError,
    ForbiddenError,
    InternalError,
    NotFoundError,
    TransientDependencyError,
)
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.services import collection_service as collection_service_module
from app.services import document_counters, kg_service, m1_file_measurement, native_derived_worker, queue_rescuer
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
from app.util.errors import INTERNAL, OPERATION_TIMEOUT, PERMISSION_DENIED, WRITE_BUSY, exception_envelope
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
    delete rolls back with its event and the request fails with `write_busy`,
    which says what this request deleted, over MCP as over REST; the first
    document stays deleted, with its event. Run again after the lock is free,
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
                with pytest.raises(TransientDependencyError) as failure:
                    await asyncio.wait_for(
                        CollectionService(git=object()).delete(
                            vault=vault, path="notes", recursive=True, agent_id="tester",
                            disclose_paths=True,
                        ),
                        timeout=_EVENT_LOCK_WAIT_SECONDS * 3,
                    )
                waited = time.monotonic() - started
            finally:
                await holding.__aexit__(None, None, None)
            monkeypatch.setattr(collection_service_module, "_delete_native_document", delete_document)

        assert isinstance(failure.value.__cause__, asyncpg.LockNotAvailableError)
        assert (failure.value.status_code, failure.value.code, failure.value.retry_after_secs) == (
            429, WRITE_BUSY, 1,
        )
        progress = {"partially_applied": True, "deleted_docs": 1, "deleted_uris": [doc_uri(vault, "notes/a.md")]}
        assert failure.value.details == progress
        envelope = exception_envelope(failure.value)
        assert (envelope["code"], envelope["details"]) == (WRITE_BUSY, {"retry_after_secs": 1, **progress})
        assert "committed deletions for" in envelope["hint"]
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
            vault=vault, path="p", recursive=True, agent_id="tester", disclose_paths=True,
        )
        assert (result["deleted_docs"], result["deleted_sub_collections"]) == (2, 1)
        # The response says which collection stayed, and why.
        assert (result["retained_collection_count"], result["retained_collections"]) == (1, ["p"])
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
            vault=vault, path="p", recursive=True, agent_id="tester", disclose_paths=True,
        )
        assert (again["deleted_docs"], again["deleted_sub_collections"]) == (0, 0)
        assert (again["retained_collections"], again["hint"]) == (["p"], result["hint"])
        # A caller who may not read the Vault is told how many, not which.
        write_only = await CollectionService(git=object()).delete(
            vault=vault, path="p", recursive=True, agent_id="tester",
        )
        assert write_only["retained_collection_count"] == 1
        assert "retained_collections" not in write_only
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

        # An internal error, which says what went.
        with pytest.raises(InternalError) as stopped:
            await delete()
        assert isinstance(stopped.value.__cause__, RuntimeError)
        assert stopped.value.details["deleted_docs"] == 2
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

        def delete(*, admin: bool, reader: bool = True):
            return CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", allow_table_delete=admin,
                disclose_paths=reader,
            )

        with pytest.raises(ForbiddenError) as partial:
            await delete(admin=False)
        uris = [doc_uri(vault, path) for path in _PATHS]
        assert partial.value.details == {"partially_applied": True, "deleted_docs": 2, "deleted_uris": uris}
        assert "'admin' role" in partial.value.hint
        assert "committed deletions for" in partial.value.hint
        envelope = exception_envelope(partial.value)
        assert (envelope["code"], envelope["details"]["deleted_uris"]) == (PERMISSION_DENIED, uris)
        assert envelope["hint"] == partial.value.hint
        assert await _state(pool, vault_id) == {
            "documents": [(path, "deleted") for path in _PATHS],
            "collections": ["notes"],
            "events": ["document.delete", "document.delete"],
        }

        with pytest.raises(ForbiddenError) as again:
            await delete(admin=False, reader=False)
        assert again.value.details is None

        result = await delete(admin=True)
        assert (result["deleted_docs"], result["deleted_tables"]) == (0, 1)
        assert (await _state(pool, vault_id))["collections"] == []
        assert await pool.fetchval("SELECT to_regclass($1)", created[0]) is None


async def test_a_table_refusal_tells_a_write_only_caller_counts_not_paths(monkeypatch):
    """The same late table, for a caller who may write the Vault but not read
    it: the refusal says how many documents this request deleted, not which."""
    async with _fresh_database() as pool:
        vault = "native-table-write-only"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        check = CollectionService._refuse_tables_under

        async def table_created_after_the_check(self, vault_name, vid, norm):
            await check(self, vault_name, vid, norm)
            await _late_table(pool, vault, vault_id, "notes")

        monkeypatch.setattr(CollectionService, "_refuse_tables_under", table_created_after_the_check)
        with pytest.raises(ForbiddenError) as partial:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester",
            )
        assert partial.value.details == {"partially_applied": True, "deleted_docs": 2}
        assert "2 document(s)" in partial.value.hint
        assert not any(path in partial.value.hint for path in _PATHS)
        envelope = exception_envelope(partial.value)
        assert "deleted_uris" not in envelope["details"]


async def _late_table(pool, vault: str, vault_id: uuid.UUID, collection: str) -> None:
    """A table another request creates under ``collection``, once."""
    if await pool.fetchval("SELECT 1 FROM vault_tables WHERE vault_id = $1 AND name = 'late'", vault_id):
        return
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
            "'[]'::jsonb, (SELECT id FROM collections WHERE vault_id = $2 AND path = $4))",
            uuid.uuid4(), vault_id, '[{"name": "value", "type": "text"}]', collection,
        )


async def test_a_restored_document_stays_listed_as_deleted_by_this_request(monkeypatch):
    """The delete committed `notes/a.md` and `notes/b.md`. Before its last
    transaction another request restored `notes/a.md` and created a table
    under `notes`, so the delete started another round, whose table check
    refuses it. The refusal lists both documents as the ones this request
    committed deletions for, and its hint says later writes or restores may
    have changed them: `notes/a.md` is live again."""
    async with _fresh_database() as pool:
        vault = "native-restored-in-between"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        last_transaction = CollectionService._delete_collections
        changed: list[bool] = []

        async def restore_and_add_a_table(self, **kwargs):
            if not changed:
                changed.append(True)
                row = await pool.fetchrow(
                    "SELECT resource_id, head_revision_id FROM native_resources "
                    "WHERE namespace_id = $1 AND current_path = $2",
                    vault_id, _PATHS[0],
                )
                await native.restore_text(
                    namespace_id=vault_id, surface="document", path=_PATHS[0], payload="restored",
                    actor="other", mutation_id=uuid.uuid4(), expected_revision_id=row["head_revision_id"],
                    expected_resource_id=row["resource_id"],
                )
                await _late_table(pool, vault, vault_id, "notes")
            return await last_transaction(self, **kwargs)

        monkeypatch.setattr(CollectionService, "_delete_collections", restore_and_add_a_table)
        with pytest.raises(ForbiddenError) as partial:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert partial.value.details["deleted_uris"] == [doc_uri(vault, path) for path in _PATHS]
        assert "committed deletions for" in partial.value.hint
        assert "later writes or restores may have changed them since" in partial.value.hint
        assert (await _state(pool, vault_id))["documents"] == [(_PATHS[0], "live"), (_PATHS[1], "deleted")]


async def test_a_delete_whose_rounds_run_out_says_what_it_deleted(monkeypatch):
    """Before each round's last transaction another request writes a document
    under `notes`, so every round finds one and the delete gives up after its
    third with a conflict. The conflict says the delete partly completed and,
    to a caller who may read the Vault, lists the four documents this request
    committed deletions for, over MCP as over REST; the document written last
    stays."""
    async with _fresh_database() as pool:
        vault = "native-rounds-run-out"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        last_transaction = CollectionService._delete_collections
        arrived: list[str] = []

        async def a_document_arrives(self, **kwargs):
            path = f"notes/late-{len(arrived)}.md"
            await native.create_text(
                namespace_id=vault_id, surface="document", path=path,
                payload="written meanwhile", actor="other", mutation_id=uuid.uuid4(),
            )
            arrived.append(path)
            return await last_transaction(self, **kwargs)

        monkeypatch.setattr(CollectionService, "_delete_collections", a_document_arrives)

        def delete(reader: bool):
            return CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=reader,
            )

        with pytest.raises(ConflictError) as ran_out:
            await delete(reader=True)
        deleted = [*_PATHS, *arrived[:2]]
        assert ran_out.value.details == {
            "partially_applied": True,
            "deleted_docs": 4,
            "deleted_uris": [doc_uri(vault, path) for path in deleted],
        }
        assert "committed deletions for" in ran_out.value.hint
        envelope = exception_envelope(ran_out.value)
        assert (envelope["code"], envelope["details"]["deleted_docs"]) == ("conflict", 4)
        assert envelope["hint"] == ran_out.value.hint
        assert (await _state(pool, vault_id))["documents"][-1] == (arrived[2], "live")

        arrived.clear()
        with pytest.raises(ConflictError) as write_only:
            await delete(reader=False)
        assert write_only.value.details == {"partially_applied": True, "deleted_docs": 3}


async def test_a_stale_final_claim_does_not_abandon_the_reopened_intent(monkeypatch):
    """A worker made the final claim of `notes/a.md`'s delete intent and
    stalled. The lease expired, the queue rescuer abandoned the intent, and an
    operator re-opened it. The stalled worker then fails, or finishes: its
    claim is stale, so neither its failure nor its work changes the
    re-opened intent, which a new claim applies."""
    async with _fresh_database() as pool:
        vault_id, first, indexed = await _indexed_then_deleted(pool, monkeypatch, "native-stale-claim")
        await pool.execute(
            "UPDATE native_invalidation_intents SET retry_count = $1 "
            "WHERE resource_id = $2 AND completed_at IS NULL",
            MAX_RETRIES - 1, first,
        )
        worker = NativeDerivedWorker(pool)
        stale = None
        while True:
            claim = await worker._claim_one()
            assert claim is not None
            if claim["resource_id"] == first:
                stale = claim
                break
            # The other document's intent: apply it as usual.
            await worker._apply_delete(claim)
        assert stale["retry_count"] == MAX_RETRIES
        await pool.execute(
            "UPDATE native_invalidation_intents SET next_attempt_at = NOW() - INTERVAL '1 second' "
            "WHERE resource_id = $1 AND completed_at IS NULL",
            first,
        )
        assert await queue_rescuer._rescue_once() == 1
        assert await worker.requeue_abandoned(namespace_id=vault_id, resource_id=first) == 1

        # The stalled worker resumes: its failure, and its work, are stale.
        await worker._failure(stale, asyncpg.exceptions.DeadlockDetectedError("deadlock detected"))
        assert await _open_intents(pool, first) == [(0, None)]
        await worker._apply_delete(stale)
        assert await _open_intents(pool, first) == [(0, None)]
        assert await _derived(pool, first) == indexed

        await _drain(worker)
        assert await _last_outcome(pool, first) == "deleted"
        assert (await _derived(pool, first))["chunks"] == []


async def test_collections_changing_after_the_documents_went_says_what_it_deleted(monkeypatch):
    """`p` has no collection row; `p/old` holds a Native document. The delete
    deletes the document, and then another request creates a collection under
    `p` between each read and lock of the collections, three times: the
    delete gives up with a conflict. The conflict says what this request
    deleted, as every error after its first document does, and keeps its own
    code."""
    async with _fresh_database() as pool:
        vault = "native-collections-churn"
        monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: True)
        vault_id = await _make_vault(pool, vault)
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        await native.create_text(
            namespace_id=vault_id, surface="document", path="p/old/a.md",
            payload="body", actor="tester", mutation_id=uuid.uuid4(),
        )
        await CollectionRepository(pool).get_or_create(vault_id, "p/old")
        lock_rows = collection_service_module.lock_collection_rows
        created: list[str] = []

        async def a_collection_appears(conn, ids, **kwargs):
            locked = await lock_rows(conn, ids, **kwargs)
            path = f"p/new-{len(created)}"
            await CollectionRepository(pool).get_or_create(vault_id, path)
            created.append(path)
            return locked

        monkeypatch.setattr(collection_service_module, "lock_collection_rows", a_collection_appears)
        with pytest.raises(ConflictError) as churned:
            await CollectionService(git=object()).delete(
                vault=vault, path="p", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert "kept changing" in str(churned.value)
        assert churned.value.details == {
            "partially_applied": True, "deleted_docs": 1, "deleted_uris": [doc_uri(vault, "p/old/a.md")],
        }
        envelope = exception_envelope(churned.value)
        assert (envelope["code"], envelope["details"]["deleted_docs"]) == ("conflict", 1)
        assert "committed deletions for" in envelope["hint"]


async def test_a_table_drop_refused_by_a_dependency_says_what_it_deleted(monkeypatch):
    """An admin's recursive delete of `notes` deletes its documents and then
    cannot drop its table, which another table references: a conflict, which
    says what this request deleted."""
    async with _fresh_database() as pool:
        vault = "native-dependent-table"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        await _late_table(pool, vault, vault_id, "notes")
        target = table_data_repo.pg_table_name(vault, "late")
        await pool.execute("CREATE TABLE dependent_rows (late_id uuid REFERENCES " + target + "(id))")
        with pytest.raises(ConflictError) as refused:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", allow_table_delete=True,
            )
        assert "referenced by another vault table" in str(refused.value)
        assert refused.value.details == {"partially_applied": True, "deleted_docs": 2}
        assert "2 document(s)" in refused.value.hint
        assert (await _state(pool, vault_id))["documents"] == [(path, "deleted") for path in _PATHS]


async def test_a_document_restored_and_deleted_again_counts_once(monkeypatch):
    """The delete deleted `notes/a.md` and `notes/b.md`; before its last
    transaction another request restored `notes/a.md`, so the delete's next
    round deleted it again. The response counts two documents, as does the
    `collection.delete` event: the same document deleted twice counts once."""
    async with _fresh_database() as pool:
        vault = "native-deleted-twice"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        last_transaction = CollectionService._delete_collections
        restored: list[bool] = []

        async def restore_once(self, **kwargs):
            if not restored:
                restored.append(True)
                row = await pool.fetchrow(
                    "SELECT resource_id, head_revision_id FROM native_resources "
                    "WHERE namespace_id = $1 AND current_path = $2",
                    vault_id, _PATHS[0],
                )
                await native.restore_text(
                    namespace_id=vault_id, surface="document", path=_PATHS[0], payload="restored",
                    actor="other", mutation_id=uuid.uuid4(), expected_revision_id=row["head_revision_id"],
                    expected_resource_id=row["resource_id"],
                )
            return await last_transaction(self, **kwargs)

        monkeypatch.setattr(CollectionService, "_delete_collections", restore_once)
        result = await CollectionService(git=object()).delete(
            vault=vault, path="notes", recursive=True, agent_id="tester",
        )
        assert result["deleted_docs"] == 2
        payload = await pool.fetchval(
            "SELECT payload FROM events WHERE vault_id = $1 AND kind = 'collection.delete'", vault_id,
        )
        assert '"deleted_docs": 2' in payload


async def test_a_recursive_delete_alongside_a_vault_delete_does_not_deadlock(tmp_path, monkeypatch):
    """A Vault delete has locked the Vault and deleted its edges when a
    recursive delete of `notes` starts; the Vault delete then deletes the Vault,
    which cascades to its Resources. Each per-document transaction of the
    recursive delete locks the Vault before the Resource, as every child
    write does, so it waits for the Vault delete instead of holding a Resource
    the cascade needs: the Vault delete completes, and the recursive delete
    fails because the Vault is gone. Locking the Resource first made the two
    wait for each other until PostgreSQL ended one with a deadlock."""
    from app.config import settings
    from app.services import access_service, index_service

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    async with _fresh_database() as pool:
        vault = "native-vault-delete-race"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        await native.create_text(
            namespace_id=vault_id, surface="document", path="other/c.md",
            payload="body of other/c.md", actor="tester", mutation_id=uuid.uuid4(),
        )
        await _link(vault, _PATHS[0], "other/c.md")
        admin = await pool.fetchval(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES ($1, 'race-admin', 'race-admin@example.com', '!fixture!', TRUE) RETURNING id",
            uuid.uuid4(),
        )

        class _NoRoles:
            def __getattr__(self, _name):
                async def nothing(*_args, **_kwargs):
                    return None
                return nothing

        monkeypatch.setattr(access_service, "get_role_sync", lambda: _NoRoles())
        edges_gone, resume = asyncio.Event(), asyncio.Event()
        drop_chunks = index_service.delete_vault_chunks

        async def pause_after_the_edges(conn, vid):
            edges_gone.set()
            await resume.wait()
            return await drop_chunks(conn, vid)

        monkeypatch.setattr(index_service, "delete_vault_chunks", pause_after_the_edges)
        deleting_vault = asyncio.create_task(access_service.delete_vault(str(admin), vault))
        await asyncio.wait_for(edges_gone.wait(), timeout=10)
        deleting_notes = asyncio.create_task(CollectionService(git=object()).delete(
            vault=vault, path="notes", recursive=True, agent_id="tester",
        ))

        async def waiting_for_a_lock() -> None:
            # A row lock wait is on the holder's transaction id, which
            # pg_locks does not tie to a database: read the waiter instead.
            while not await pool.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ):
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(waiting_for_a_lock(), timeout=10)
        finally:
            resume.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(deleting_vault, deleting_notes, return_exceptions=True), timeout=30,
        )
        print(f"\nvault delete: {outcomes[0]!r}; recursive delete: {outcomes[1]!r}")
        assert not any(isinstance(o, asyncpg.DeadlockDetectedError) for o in outcomes), outcomes
        assert not isinstance(outcomes[0], BaseException), outcomes[0]
        assert await pool.fetchval("SELECT count(*) FROM vaults WHERE id = $1", vault_id) == 0
        assert isinstance(outcomes[1], ConflictError), outcomes[1]


async def _rest_answer(exc: Exception, monkeypatch) -> tuple[int, dict]:
    """The REST status and body the API gives for ``exc``."""
    import sys
    import tempfile

    from starlette.requests import Request

    from app.config import settings

    if "app.main" not in sys.modules:
        # Importing the app builds route services under the Git storage
        # path, which must exist.
        monkeypatch.setattr(settings, "git_storage_path", tempfile.mkdtemp(prefix="akb-rest-answer-"))
    from app.main import akb_error_handler

    request = Request({
        "type": "http", "method": "DELETE", "path": "/api/v1/collections/v/notes",
        "headers": [], "query_string": b"",
    })
    response = await akb_error_handler(request, exc)
    return response.status_code, json.loads(response.body)


async def _assert_internal_with_progress(
    exc: Exception, vault: str, paths: tuple[str, ...], monkeypatch,
) -> None:
    """``exc`` is an internal error, HTTP 500 and code `internal` with the
    generic message over REST and MCP, that says what the delete deleted."""
    progress = {
        "partially_applied": True, "deleted_docs": len(paths),
        "deleted_uris": [doc_uri(vault, path) for path in paths],
    }
    assert isinstance(exc, InternalError)
    assert (exc.status_code, exc.code, exc.details) == (500, "internal", progress)
    # The generic message, exactly; what went is in the hint and details.
    assert str(exc) == "Internal server error"
    status, body = await _rest_answer(exc, monkeypatch)
    assert (status, body["code"], body["details"]) == (500, INTERNAL, progress), body
    assert body["message"] == "Internal server error", body
    assert "committed deletions for" in body["hint"], body
    envelope = exception_envelope(exc)
    assert (envelope["code"], envelope["details"]) == (INTERNAL, progress), envelope
    assert envelope["error"] == "Internal server error", envelope
    assert "committed deletions for" in envelope["hint"], envelope


async def test_a_plain_lock_timeout_after_the_documents_went_is_internal_and_says_what_it_deleted(monkeypatch):
    """Both documents went. The last transaction then waits for the `notes`
    collection row, which another transaction holds, under a lock timeout
    of its own, as a database role's lock_timeout would set: PostgreSQL ends
    the wait with a plain 55P03, not the Vault's event lock, so it stays an
    internal error. It is reported as one, with the generic message, over
    REST and MCP, and says what the request deleted; the lock timeout is its
    cause. Such an error used to be returned as it was, without saying what
    had gone."""
    async with _fresh_database() as pool:
        vault = "native-plain-lock-timeout"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        lock_rows = collection_service_module.lock_collection_rows

        async with pool.acquire() as holder:
            async with _OpenTransaction(holder):
                held: list[bool] = []

                async def lock_with_a_timeout(conn, *args, **kwargs):
                    if not held:
                        # The documents are gone: hold the row now.
                        await holder.execute(
                            "SELECT 1 FROM collections WHERE vault_id = $1 AND path = 'notes' FOR UPDATE",
                            vault_id,
                        )
                        held.append(True)
                    await conn.execute("SET LOCAL lock_timeout = '200ms'")
                    return await lock_rows(conn, *args, **kwargs)

                monkeypatch.setattr(collection_service_module, "lock_collection_rows", lock_with_a_timeout)
                with pytest.raises(AKBError) as failure:
                    await CollectionService(git=object()).delete(
                        vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
                    )
        cause = failure.value.__cause__
        assert isinstance(cause, asyncpg.LockNotAvailableError), repr(cause)
        assert getattr(cause, "constraint_name", None) != "akb_event_lock_timeout"
        await _assert_internal_with_progress(failure.value, vault, _PATHS, monkeypatch)
        assert await _state(pool, vault_id) == {
            "documents": [(path, "deleted") for path in _PATHS],
            "collections": ["notes"],
            "events": ["document.delete", "document.delete"],
        }


async def test_a_deadlock_after_the_first_document_is_internal_and_says_what_it_deleted(monkeypatch):
    """The first document went; the second's delete then ends in a
    deadlock (40P01). The request answers with an internal error, with the
    generic message, over REST and MCP, that says the first document went;
    the deadlock is its cause."""
    async with _fresh_database() as pool:
        vault = "native-deadlock"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        delete_document = collection_service_module._delete_native_document
        calls: list[bool] = []

        async def deadlock_on_the_second(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
            return await delete_document(*args, **kwargs)

        monkeypatch.setattr(collection_service_module, "_delete_native_document", deadlock_on_the_second)
        with pytest.raises(AKBError) as failure:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert isinstance(failure.value.__cause__, asyncpg.exceptions.DeadlockDetectedError)
        await _assert_internal_with_progress(failure.value, vault, _PATHS[:1], monkeypatch)
        assert (await _state(pool, vault_id))["documents"] == [(_PATHS[0], "deleted"), (_PATHS[1], "live")]


async def test_a_deletion_is_recorded_once_committed_though_the_connection_reset_fails(monkeypatch):
    """The first document's delete commits, and then the pool fails to reset
    the connection it ran on, as a reset that times out does. The error
    still says the document went: each deletion is recorded as soon as its
    transaction commits, before the connection goes back. It used to be
    recorded after that, so this error said nothing of it."""
    async with _fresh_database() as pool:
        vault = "native-reset-fails"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        in_delete: list[bool] = []
        failed: list[bool] = []
        edges = collection_service_module.delete_native_document_edges

        async def delete_edges(*args, **kwargs):
            in_delete.append(True)
            return await edges(*args, **kwargs)

        class _ResetFails:
            """The pool's own acquisition, whose release then fails once,
            after the first document's delete transaction."""

            def __init__(self) -> None:
                self._acquired = pool.acquire()

            async def __aenter__(self):
                return await self._acquired.__aenter__()

            async def __aexit__(self, *exc):
                outcome = await self._acquired.__aexit__(*exc)
                if in_delete and not failed and exc[0] is None:
                    failed.append(True)
                    raise TimeoutError("timed out resetting the connection")
                return outcome

        class _Pool:
            def acquire(self):
                return _ResetFails()

        async def get_pool():
            return _Pool()

        monkeypatch.setattr(collection_service_module, "delete_native_document_edges", delete_edges)
        monkeypatch.setattr(collection_service_module, "get_pool", get_pool)
        with pytest.raises(TransientDependencyError) as failure:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert failed
        assert isinstance(failure.value.__cause__, TimeoutError)
        assert (failure.value.code, failure.value.details) == (OPERATION_TIMEOUT, {
            "partially_applied": True, "deleted_docs": 1, "deleted_uris": [doc_uri(vault, _PATHS[0])],
        })
        assert await _state(pool, vault_id) == {
            "documents": [(_PATHS[0], "deleted"), (_PATHS[1], "live")],
            "collections": ["notes"],
            "events": ["document.delete"],
        }


async def test_a_file_delete_refused_after_the_documents_went_keeps_its_details_over_mcp(monkeypatch):
    """`notes` holds the two documents and a native_text File, and no
    native_text File delete service is registered in this process. The
    documents go, and the last transaction's File delete then fails with a
    503 AKB error. Over MCP, as over REST, the error keeps the details and
    hint that say what the request deleted, and its code stays `internal`;
    the MCP envelope used to drop them for an AKB error of no other kind."""
    async with _fresh_database() as pool:
        vault = "native-text-file"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        notes = await pool.fetchval(
            "SELECT id FROM collections WHERE vault_id = $1 AND path = 'notes'", vault_id,
        )
        await pool.execute(
            "INSERT INTO vault_files (vault_id, collection_id, name, s3_key, storage_driver, "
            "storage_locator, native_resource_id, native_revision_id) "
            "VALUES ($1, $2, 'n.txt', 'notes/n.txt', 'native_text', 'native-text:n', $3, $4)",
            vault_id, notes, uuid.uuid4(), "a" * 40,
        )
        monkeypatch.setattr(m1_file_measurement, "_native_text_deleter", None)
        with pytest.raises(AKBError) as failure:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        progress = {
            "partially_applied": True, "deleted_docs": 2,
            "deleted_uris": [doc_uri(vault, path) for path in _PATHS],
        }
        assert (failure.value.status_code, failure.value.details) == (503, progress)
        envelope = exception_envelope(failure.value)
        assert (envelope["code"], envelope["details"]) == (INTERNAL, progress), envelope
        assert "committed deletions for" in envelope["hint"], envelope
        status, body = await _rest_answer(failure.value, monkeypatch)
        assert (status, body["details"]) == (503, progress), body


class _Connection:
    """A pool connection whose outermost transaction's COMMIT goes wrong once
    ``armed`` and not yet ``fired``. With ``failure`` "lost" the exit commits
    and then raises ConnectionResetError, as a connection lost before the
    database's answer would; with "closed" it commits and raises asyncpg's
    own ConnectionDoesNotExistError (SQLSTATE 08003), which asyncpg raises
    when the connection closes mid-operation; with "refused" it rolls back
    and raises the error the database answered with."""

    def __init__(self, conn, armed: list[bool], fired: list[bool], failure: str = "lost") -> None:
        self._conn = conn
        self._armed = armed
        self._fired = fired
        self._failure = failure
        self._depth = 0

    def transaction(self, *args, **kwargs):
        return _AnswerLost(self, self._conn.transaction(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._conn, name)


# The error each ``failure`` raises at the transaction's exit.
_COMMIT_FAILURES = {
    "lost": ConnectionResetError,
    "closed": asyncpg.exceptions.ConnectionDoesNotExistError,
    "refused": asyncpg.exceptions.SerializationError,
}


class _AnswerLost:
    def __init__(self, conn: _Connection, transaction) -> None:
        self._conn = conn
        self._transaction = transaction

    async def __aenter__(self):
        self._conn._depth += 1
        return await self._transaction.__aenter__()

    async def __aexit__(self, *exc):
        self._conn._depth -= 1
        firing = (
            self._conn._depth == 0 and exc[0] is None and self._conn._armed and not self._conn._fired
        )
        if firing and self._conn._failure == "refused":
            self._conn._fired.append(True)
            refusal = asyncpg.exceptions.SerializationError("could not serialize access")
            await self._transaction.__aexit__(type(refusal), refusal, None)
            raise refusal
        outcome = await self._transaction.__aexit__(*exc)
        if firing:
            self._conn._fired.append(True)
            if self._conn._failure == "closed":
                raise asyncpg.exceptions.ConnectionDoesNotExistError(
                    "connection was closed in the middle of operation",
                )
            raise ConnectionResetError("the connection was lost before COMMIT was answered")
        return outcome


def _commit_goes_wrong(monkeypatch, pool, *, failure: str, armed: list[bool]) -> list[bool]:
    """Route the delete's connections through `_Connection`, whose outermost
    transaction's COMMIT goes wrong once ``armed`` is set; returns the list
    that says it did."""
    fired: list[bool] = []

    class _Acquire:
        def __init__(self) -> None:
            self._acquired = pool.acquire()

        async def __aenter__(self):
            return _Connection(await self._acquired.__aenter__(), armed, fired, failure)

        async def __aexit__(self, *exc):
            return await self._acquired.__aexit__(*exc)

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(collection_service_module, "get_pool", get_pool)
    return fired


def _commit_goes_wrong_on_the_first_document(monkeypatch, pool, *, failure: str) -> list[bool]:
    """Make the first document's delete transaction's COMMIT go wrong (see
    `_Connection`); returns the list that says it did."""
    armed: list[bool] = []
    edges = collection_service_module.delete_native_document_edges

    async def delete_edges(*args, **kwargs):
        armed.append(True)
        return await edges(*args, **kwargs)

    monkeypatch.setattr(collection_service_module, "delete_native_document_edges", delete_edges)
    return _commit_goes_wrong(monkeypatch, pool, failure=failure, armed=armed)


@pytest.mark.parametrize("lost", ["lost", "closed"])
async def test_a_deletion_whose_commit_answer_is_lost_is_reported_as_uncertain(monkeypatch, lost):
    """The first document's delete commits, and the answer to its COMMIT is
    then lost with the connection: the transaction's exit raises, with a
    plain connection reset or with asyncpg's ConnectionDoesNotExistError (a
    connection error, SQLSTATE class 08, not the database's answer). The
    deletion may have committed, and here it did. The error says the request
    partly applied, counts that document as uncertain and, to a reader,
    names it, and its hint says the outcome is unknown. It used to say
    nothing at all of the request's progress, and asyncpg's error, a
    PostgresError, was taken for the database refusing the commit."""
    async with _fresh_database() as pool:
        vault = "native-commit-unanswered"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        fired = _commit_goes_wrong_on_the_first_document(monkeypatch, pool, failure=lost)
        with pytest.raises(TransientDependencyError) as failure:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert fired
        assert isinstance(failure.value.__cause__, _COMMIT_FAILURES[lost])
        assert failure.value.details == {
            "partially_applied": True, "deleted_docs": 0, "deleted_uris": [],
            "uncertain_docs": 1, "uncertain_uris": [doc_uri(vault, _PATHS[0])],
        }
        assert "is unknown" in failure.value.hint
        envelope = exception_envelope(failure.value)
        assert envelope["details"]["uncertain_uris"] == [doc_uri(vault, _PATHS[0])]
        # It did commit, with its event.
        assert await _state(pool, vault_id) == {
            "documents": [(_PATHS[0], "deleted"), (_PATHS[1], "live")],
            "collections": ["notes"],
            "events": ["document.delete"],
        }


async def test_a_commit_the_database_refused_is_not_reported_as_uncertain(monkeypatch):
    """The first document's COMMIT is refused by the database, which rolls
    the transaction back. That deletion did not happen, so it is not
    uncertain: the request had done nothing yet, and the error is the
    database's own."""
    async with _fresh_database() as pool:
        vault = "native-commit-refused"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        fired = _commit_goes_wrong_on_the_first_document(monkeypatch, pool, failure="refused")
        with pytest.raises(asyncpg.exceptions.SerializationError):
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert fired
        assert await _state(pool, vault_id) == {
            "documents": [(path, "live") for path in _PATHS],
            "collections": ["notes"],
            "events": [],
        }


async def test_a_delete_whose_last_transaction_committed_answers_success_though_the_reset_fails(
    monkeypatch, caplog,
):
    """`notes` holds the two documents and a File. The delete's last
    transaction commits the File's and the collection's deletion, with
    `collection.delete`, and then the pool fails to reset its connection. The
    delete is complete and answers so, the File counted; the failure is
    logged. It used to answer with an error that said only that the
    documents went."""
    async with _fresh_database() as pool:
        vault = "native-last-reset-fails"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        notes = await pool.fetchval(
            "SELECT id FROM collections WHERE vault_id = $1 AND path = 'notes'", vault_id,
        )
        await pool.execute(
            "INSERT INTO vault_files (vault_id, collection_id, name, s3_key) "
            "VALUES ($1, $2, 'n.bin', 'notes/n.bin')",
            vault_id, notes,
        )
        in_last: list[bool] = []
        failed: list[bool] = []
        lock_rows = collection_service_module.lock_collection_rows

        async def lock_in_the_last_transaction(*args, **kwargs):
            in_last.append(True)
            return await lock_rows(*args, **kwargs)

        class _ResetFails:
            def __init__(self) -> None:
                self._acquired = pool.acquire()

            async def __aenter__(self):
                return await self._acquired.__aenter__()

            async def __aexit__(self, *exc):
                outcome = await self._acquired.__aexit__(*exc)
                if in_last and not failed and exc[0] is None:
                    failed.append(True)
                    raise TimeoutError("timed out resetting the connection")
                return outcome

        class _Pool:
            def acquire(self):
                return _ResetFails()

        async def get_pool():
            return _Pool()

        monkeypatch.setattr(collection_service_module, "lock_collection_rows", lock_in_the_last_transaction)
        monkeypatch.setattr(collection_service_module, "get_pool", get_pool)
        with caplog.at_level("ERROR", logger="akb.collections"):
            result = await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert failed
        assert result == {
            "ok": True, "collection": "notes", "deleted_docs": 2, "deleted_files": 1,
            "deleted_sub_collections": 0, "deleted_tables": 0,
        }
        assert any("giving its connection back failed" in r.getMessage() for r in caplog.records)
        assert await _state(pool, vault_id) == {
            "documents": [(path, "deleted") for path in _PATHS],
            "collections": [],
            "events": ["document.delete", "document.delete", "collection.delete"],
        }
        assert await pool.fetchval("SELECT count(*) FROM vault_files WHERE vault_id = $1", vault_id) == 0


async def test_a_file_whose_resource_is_gone_keeps_the_delete_progress_over_rest(monkeypatch):
    """`notes` holds the two documents and a native_text File whose Native
    Resource is gone, so the File's delete raises not found after the
    documents went. The REST delete answers 404 with the details and hint
    that say what the request deleted. The route used to turn the error into
    a bare 404 that kept only its message."""
    import sys
    import tempfile

    import httpx
    from fastapi import FastAPI
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.config import settings

    if "app.main" not in sys.modules or "app.api.routes.collections" not in sys.modules:
        monkeypatch.setattr(settings, "git_storage_path", tempfile.mkdtemp(prefix="akb-rest-delete-"))
    from app.api.deps import get_current_user
    from app.api.routes import collections as collection_routes
    from app.main import akb_error_handler, http_error_handler

    async with _fresh_database() as pool:
        vault = "native-file-resource-gone"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        notes = await pool.fetchval(
            "SELECT id FROM collections WHERE vault_id = $1 AND path = 'notes'", vault_id,
        )
        await pool.execute(
            "INSERT INTO vault_files (vault_id, collection_id, name, s3_key, storage_driver, "
            "storage_locator, native_resource_id, native_revision_id) "
            "VALUES ($1, $2, 'n.txt', 'notes/n.txt', 'native_text', 'native-text:n', $3, $4)",
            vault_id, notes, uuid.uuid4(), "b" * 40,
        )

        async def resource_gone(_conn, request):
            raise NotFoundError("Native Resource", str(request.resource_id))

        async def writer(*_args, **_kwargs):
            return {"role": "writer"}

        async def reader(*_args, **_kwargs):
            return True

        monkeypatch.setattr(m1_file_measurement, "_native_text_deleter", resource_gone)
        monkeypatch.setattr(collection_routes, "check_vault_access", writer)
        monkeypatch.setattr(collection_routes, "_can_read_vault", reader)
        app = FastAPI()
        app.include_router(collection_routes.router, prefix="/api/v1")
        app.add_exception_handler(AKBError, akb_error_handler)
        app.add_exception_handler(StarletteHTTPException, http_error_handler)
        app.dependency_overrides[get_current_user] = lambda: type("User", (), {"user_id": "tester"})()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/api/v1/collections/{vault}/notes", params={"recursive": "true"})
        assert response.status_code == 404, response.text
        body = response.json()
        assert (body["code"], body["details"]) == ("not_found", {
            "partially_applied": True, "deleted_docs": 2,
            "deleted_uris": [doc_uri(vault, path) for path in _PATHS],
        }), body
        assert "committed deletions for" in body["hint"], body


@pytest.mark.parametrize("lost", ["lost", "closed"])
async def test_a_last_step_whose_commit_answer_is_lost_is_reported_as_uncertain(monkeypatch, lost):
    """Both documents went. The delete's last transaction then commits the
    collection's deletion with `collection.delete`, and the answer to its
    COMMIT is lost with the connection: the transaction's exit raises, with a
    plain connection reset or with asyncpg's ConnectionDoesNotExistError. The
    error says, beside the documents the request deleted, that the last
    step's outcome is unknown (`final_step_uncertain`), and its hint says
    what that step covers, that what the prefix holds now does not settle
    it, and what sending the request again does. It used to report only the
    documents."""
    async with _fresh_database() as pool:
        vault = "native-last-commit-unanswered"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        armed: list[bool] = []
        lock_rows = collection_service_module.lock_collection_rows

        async def lock_in_the_last_transaction(*args, **kwargs):
            armed.append(True)
            return await lock_rows(*args, **kwargs)

        monkeypatch.setattr(collection_service_module, "lock_collection_rows", lock_in_the_last_transaction)
        fired = _commit_goes_wrong(monkeypatch, pool, failure=lost, armed=armed)
        with pytest.raises(TransientDependencyError) as failure:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert fired
        assert isinstance(failure.value.__cause__, _COMMIT_FAILURES[lost])
        assert failure.value.details == {
            "partially_applied": True, "deleted_docs": 2,
            "deleted_uris": [doc_uri(vault, path) for path in _PATHS],
            "final_step_uncertain": True,
        }
        hint = failure.value.hint
        assert "last step" in hint and "collection.delete" in hint and "Files and tables" in hint, hint
        assert "does not settle whether it went" in hint and "browse" not in hint, hint
        assert "records collection.delete at least once" in hint, hint
        envelope = exception_envelope(failure.value)
        assert envelope["details"]["final_step_uncertain"] is True
        # It did commit.
        assert await _state(pool, vault_id) == {
            "documents": [(path, "deleted") for path in _PATHS],
            "collections": [],
            "events": ["document.delete", "document.delete", "collection.delete"],
        }


async def test_a_second_deletion_of_a_restored_and_moved_document_with_its_answer_lost_is_uncertain(
    monkeypatch,
):
    """The delete deleted `notes/a.md` and `notes/b.md`. Before its last
    transaction another request restored `notes/a.md` and moved it to
    `notes/c.md`, so the delete's next round deleted that document again, at
    its new path, and the answer to that COMMIT was lost. The error counts
    the two committed deletions and, apart, the uncertain one at
    `notes/c.md`. The uncertain attempt used to be dropped because the same
    document's earlier deletion had committed."""
    async with _fresh_database() as pool:
        vault = "native-moved-then-unanswered"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        last_transaction = CollectionService._delete_collections
        moved: list[bool] = []

        async def restore_and_move_once(self, **kwargs):
            if not moved:
                moved.append(True)
                row = await pool.fetchrow(
                    "SELECT resource_id, head_revision_id FROM native_resources "
                    "WHERE namespace_id = $1 AND current_path = $2",
                    vault_id, _PATHS[0],
                )
                restored = await native.restore_text(
                    namespace_id=vault_id, surface="document", path=_PATHS[0], payload="restored",
                    actor="other", mutation_id=uuid.uuid4(), expected_revision_id=row["head_revision_id"],
                    expected_resource_id=row["resource_id"],
                )
                await native.move_text(
                    namespace_id=vault_id, surface="document", path=_PATHS[0], path_to="notes/c.md",
                    actor="other", mutation_id=uuid.uuid4(), expected_revision_id=restored.revision_id,
                    expected_resource_id=row["resource_id"],
                )
            return await last_transaction(self, **kwargs)

        armed: list[bool] = []
        edges = collection_service_module.delete_native_document_edges

        async def delete_edges(*args, **kwargs):
            if args[4] == "notes/c.md":
                armed.append(True)
            return await edges(*args, **kwargs)

        monkeypatch.setattr(CollectionService, "_delete_collections", restore_and_move_once)
        monkeypatch.setattr(collection_service_module, "delete_native_document_edges", delete_edges)
        fired = _commit_goes_wrong(monkeypatch, pool, failure="closed", armed=armed)
        with pytest.raises(TransientDependencyError) as failure:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert fired
        assert failure.value.details == {
            "partially_applied": True, "deleted_docs": 2,
            "deleted_uris": [doc_uri(vault, path) for path in _PATHS],
            "uncertain_docs": 1, "uncertain_uris": [doc_uri(vault, "notes/c.md")],
        }
        assert "is unknown" in failure.value.hint


async def test_a_bare_git_last_step_whose_commit_answer_is_lost_names_its_documents(monkeypatch):
    """On the Bare Git authority the delete's last transaction deletes the
    documents under `notes` with the collection, and the answer to its COMMIT
    is lost (asyncpg's ConnectionDoesNotExistError). The error says the last
    step's outcome is unknown, counts the documents that step deletes in
    `final_step_docs` and names them to a reader in `final_step_uris`, and
    its hint mentions them. The hint used to name only collections, Files and
    tables."""
    async with _fresh_database() as pool:
        vault = "bare-last-commit-unanswered"
        monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: False)
        vault_id = await _make_vault(pool, vault)
        notes = await CollectionRepository(pool).get_or_create(vault_id, "notes")
        for path in _PATHS:
            await pool.execute(
                "INSERT INTO documents (vault_id, collection_id, path, title) VALUES ($1, $2, $3, $3)",
                vault_id, notes, path,
            )
        armed: list[bool] = []
        lock_rows = collection_service_module.lock_collection_rows

        async def lock_in_the_last_transaction(*args, **kwargs):
            armed.append(True)
            return await lock_rows(*args, **kwargs)

        monkeypatch.setattr(collection_service_module, "lock_collection_rows", lock_in_the_last_transaction)
        fired = _commit_goes_wrong(monkeypatch, pool, failure="closed", armed=armed)
        with pytest.raises(TransientDependencyError) as failure:
            await CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester", disclose_paths=True,
            )
        assert fired
        assert failure.value.details == {
            "partially_applied": True, "deleted_docs": 0, "deleted_uris": [],
            "final_step_uncertain": True, "final_step_docs": 2,
            "final_step_uris": [doc_uri(vault, path) for path in _PATHS],
        }
        assert "details.final_step_uris" in failure.value.hint, failure.value.hint
        # It did commit.
        assert await pool.fetchval("SELECT count(*) FROM documents WHERE vault_id = $1", vault_id) == 0
        assert await pool.fetchval("SELECT count(*) FROM collections WHERE vault_id = $1", vault_id) == 0


async def _documents_gone_before_the_last_transaction(pool, monkeypatch, vault: str) -> uuid.UUID:
    """A Native Vault whose `notes` has no collection row and whose documents
    a recursive delete deleted, stopping before its last transaction: the
    next delete of `notes` finds it emptied."""
    vault_id = await _native_vault(pool, monkeypatch, vault)
    await pool.execute("DELETE FROM collections WHERE vault_id = $1", vault_id)
    last_transaction = CollectionService._delete_collections

    async def stop(self, **kwargs):
        raise RuntimeError("the delete stopped before its last transaction")

    monkeypatch.setattr(CollectionService, "_delete_collections", stop)
    with pytest.raises(InternalError):
        await CollectionService(git=object()).delete(
            vault=vault, path="notes", recursive=True, agent_id="tester",
        )
    monkeypatch.setattr(CollectionService, "_delete_collections", last_transaction)
    return vault_id


def _arm_on_the_emptied_prefix(monkeypatch) -> list[bool]:
    """Set the returned list when the last transaction finds the prefix
    emptied by an earlier delete."""
    armed: list[bool] = []
    earlier = collection_service_module._earlier_recursive_delete

    async def found_earlier(*args, **kwargs):
        armed.append(True)
        return await earlier(*args, **kwargs)

    monkeypatch.setattr(collection_service_module, "_earlier_recursive_delete", found_earlier)
    return armed


async def test_an_emptied_prefix_answers_success_though_the_reset_fails(monkeypatch, caplog):
    """The delete is run again on a prefix an earlier delete emptied. It
    records `collection.delete` and commits, and then the pool fails to
    reset its connection. It answers with its result, zero counts; the
    failure is logged. That branch used to return from inside the
    transaction, past the mark that the delete had committed, and answered
    with an error."""
    async with _fresh_database() as pool:
        vault_id = await _documents_gone_before_the_last_transaction(pool, monkeypatch, "native-emptied-reset")
        armed = _arm_on_the_emptied_prefix(monkeypatch)
        failed: list[bool] = []

        class _ResetFails:
            def __init__(self) -> None:
                self._acquired = pool.acquire()

            async def __aenter__(self):
                return await self._acquired.__aenter__()

            async def __aexit__(self, *exc):
                outcome = await self._acquired.__aexit__(*exc)
                if armed and not failed and exc[0] is None:
                    failed.append(True)
                    raise TimeoutError("timed out resetting the connection")
                return outcome

        class _Pool:
            def acquire(self):
                return _ResetFails()

        async def get_pool():
            return _Pool()

        monkeypatch.setattr(collection_service_module, "get_pool", get_pool)
        with caplog.at_level("ERROR", logger="akb.collections"):
            result = await CollectionService(git=object()).delete(
                vault="native-emptied-reset", path="notes", recursive=True, agent_id="tester",
            )
        assert failed
        assert result == {
            "ok": True, "collection": "notes", "deleted_docs": 0, "deleted_files": 0,
            "deleted_sub_collections": 0, "deleted_tables": 0,
        }
        assert any("giving its connection back failed" in r.getMessage() for r in caplog.records)
        assert (await _state(pool, vault_id))["events"] == [
            "document.delete", "document.delete", "collection.delete",
        ]


async def test_an_emptied_prefix_whose_commit_answer_is_lost_is_reported_as_uncertain(monkeypatch):
    """The delete is run again on a prefix an earlier delete emptied, and the
    answer to the COMMIT of its `collection.delete` is lost (asyncpg's
    ConnectionDoesNotExistError). The error says the last step's outcome is
    unknown. That branch used to skip the mark that its statements were done,
    so the error said nothing of it. Its hint says the prefix looks the same
    whether that step went or not, so browsing it cannot tell, and that
    sending the same request again records `collection.delete` at least once,
    maybe twice, and deletes anything written under the prefix meanwhile; it
    used to say browsing the prefix would tell."""
    async with _fresh_database() as pool:
        vault_id = await _documents_gone_before_the_last_transaction(
            pool, monkeypatch, "native-emptied-unanswered",
        )
        armed = _arm_on_the_emptied_prefix(monkeypatch)
        fired = _commit_goes_wrong(monkeypatch, pool, failure="closed", armed=armed)
        with pytest.raises(TransientDependencyError) as failure:
            await CollectionService(git=object()).delete(
                vault="native-emptied-unanswered", path="notes", recursive=True, agent_id="tester",
                disclose_paths=True,
            )
        assert fired
        assert failure.value.details == {
            "partially_applied": True, "deleted_docs": 0, "deleted_uris": [],
            "final_step_uncertain": True,
        }
        hint = failure.value.hint
        assert "already emptied the prefix" in hint, hint
        assert "looks the same whether it went or not" in hint and "browse" not in hint, hint
        assert "Sending the same request again records collection.delete at least once" in hint, hint
        assert "record it twice" in hint and "written under the prefix in the meantime" in hint, hint
        # It did commit.
        assert (await _state(pool, vault_id))["events"] == [
            "document.delete", "document.delete", "collection.delete",
        ]
