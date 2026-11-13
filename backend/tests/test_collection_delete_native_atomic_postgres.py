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
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.services import collection_service as collection_service_module
from app.services import document_counters
from app.services.collection_service import CollectionService
from app.services.git_service import GitService
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_backfill import NativeRevisionBackfill
from app.services.native_revision_cutover import CutoverVaultInput, NativeRevisionCutover
from app.services.native_revision_service import NativeRevisionService
from app.util.errors import WRITE_BUSY
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
        delete_document_chunks = collection_service_module.delete_document_chunks
        observed: list[bool] = []

        async def probe_after_the_first(conn, source_id):
            if not observed:
                async with pool.acquire() as probe:
                    async with probe.transaction():
                        free = await probe.fetchval(
                            "SELECT resource_id FROM native_resources WHERE resource_id = $1 "
                            "FOR UPDATE NOWAIT", first,
                        )
                observed.append(free == first)
            return await delete_document_chunks(conn, source_id)

        monkeypatch.setattr(collection_service_module, "delete_document_chunks", probe_after_the_first)
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
