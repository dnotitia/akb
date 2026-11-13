"""Collection deletion selects its document authority without Native Git I/O."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import collection_service


def _fixture(monkeypatch, *, native: bool, legacy: bool = False):
    vault_id, resource_id, revision_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = []
    native_rows = [{"resource_id": resource_id, "path": "c/native.md"}]
    legacy_rows = [{"id": uuid.uuid4(), "path": "c/legacy.md"}] if legacy else []
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": uuid.uuid4()})
    conn.execute = AsyncMock()
    # No Native cutover fence in this fixture.
    conn.fetchval = AsyncMock(return_value=None)
    native_deleted: list[uuid.UUID] = []

    async def fetch(sql, *args):
        if "FROM native_resources" in sql:
            return [] if native_deleted else native_rows
        return []

    conn.fetch = AsyncMock(side_effect=fetch)

    @asynccontextmanager
    async def transaction(*, isolation=None):
        assert isolation in (None, "read_committed")
        yield conn
        # The delete's own transaction sets its isolation; a savepoint inside
        # it (the collection lock attempt) does not and is not a commit.
        if isolation is not None:
            events.append("commit")

    @asynccontextmanager
    async def acquire():
        yield conn

    conn.transaction = transaction
    pool = SimpleNamespace(acquire=acquire)
    vault_repo = SimpleNamespace(get_id_by_name=AsyncMock(return_value=vault_id))
    coll_repo = SimpleNamespace(
        list_docs_under=AsyncMock(return_value=legacy_rows),
        list_files_under=AsyncMock(return_value=[]),
        list_tables_under=AsyncMock(return_value=[]),
    )
    svc = collection_service.CollectionService()
    monkeypatch.setattr(svc, "_repos", AsyncMock(return_value=(vault_repo, coll_repo)))
    monkeypatch.setattr(collection_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(collection_service, "lock_vault_for_child_write", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.services.document_counters.native_documents_are_authoritative", lambda: native,
    )
    for name in (
        "delete_document_chunks",
        "delete_document_relations",
        "delete_native_document_edges",
        "emit_event",
    ):
        monkeypatch.setattr(collection_service, name, AsyncMock())
    legacy_delete = AsyncMock(return_value=True)
    monkeypatch.setattr(collection_service.DocumentRepository, "delete_with_publications", legacy_delete)
    monkeypatch.setattr("app.services.notification_producer.enqueue_document_change", AsyncMock())
    # The collection's events are collected and inserted through events_repo.
    monkeypatch.setattr("app.repositories.events_repo.emit_event", AsyncMock())
    async def delete_resource(**kwargs):
        native_deleted.append(kwargs["expected_resource_id"])

    native_delete = AsyncMock(side_effect=delete_resource)
    locked_row = {
        "resource_id": resource_id, "namespace_id": vault_id, "surface": "document",
        "lifecycle": "live", "current_path": "c/native.md", "head_revision_id": revision_id,
    }
    repository = SimpleNamespace(lock_resource=AsyncMock(return_value=locked_row))
    native_svc = SimpleNamespace(
        _native=AsyncMock(return_value=SimpleNamespace(
            delete_resource=native_delete, repository=repository,
        )),
    )
    monkeypatch.setattr("app.services.native_document_service.NativeDocumentService", lambda: native_svc)

    @asynccontextmanager
    async def lane(vault):
        events.append("lane")
        yield

    lane_spy = MagicMock(side_effect=lane)
    monkeypatch.setattr(collection_service, "write_lane", lane_spy)
    git = MagicMock()
    git_factory = MagicMock(return_value=git)
    monkeypatch.setattr(collection_service, "GitService", git_factory)
    git_write = AsyncMock()
    monkeypatch.setattr(collection_service, "run_git_write", git_write)
    return SimpleNamespace(
        svc=svc, conn=conn, events=events, native_delete=native_delete,
        legacy_delete=legacy_delete, lane=lane_spy, git_factory=git_factory,
        git_write=git_write, git=git,
    )


async def test_native_nonempty_collection_requires_recursive(monkeypatch):
    f = _fixture(monkeypatch, native=True)
    with pytest.raises(collection_service.CollectionNotEmptyError) as error:
        await f.svc.delete(vault="v", path="c", recursive=False, agent_id=None)
    assert error.value.doc_count == 1
    f.native_delete.assert_not_awaited()
    assert all("pg_advisory_xact_lock" in call.args[0] for call in f.conn.execute.await_args_list)
    f.lane.assert_not_called()
    f.git_factory.assert_not_called()


@pytest.mark.parametrize("legacy", [False, True])
async def test_native_cascade_never_enters_git_even_with_legacy_catalog_rows(monkeypatch, legacy):
    f = _fixture(monkeypatch, native=True, legacy=legacy)
    result = await f.svc.delete(vault="v", path="c", recursive=True, agent_id="actor")
    assert result["ok"] is True
    assert result["deleted_docs"] == 1 + int(legacy)
    f.native_delete.assert_awaited_once()
    assert f.legacy_delete.await_count == int(legacy)
    assert f.events == ["commit"]
    # The service catches post-commit Git exceptions, so assert calls rather
    # than relying on a forbidden-call exception to escape the service.
    f.lane.assert_not_called()
    f.git_factory.assert_not_called()
    f.git_write.assert_not_awaited()


async def test_bare_git_cascade_keeps_post_commit_cleanup(monkeypatch):
    f = _fixture(monkeypatch, native=False, legacy=True)
    result = await f.svc.delete(vault="v", path="c", recursive=True, agent_id="actor")
    assert result["deleted_docs"] == 1
    f.native_delete.assert_not_awaited()
    f.legacy_delete.assert_awaited_once()
    assert f.events == ["commit", "lane"]
    f.git_write.assert_awaited_once()
    assert f.git_write.call_args.args == (f.git.delete_paths_bulk,)
    assert f.git_write.call_args.kwargs["file_paths"] == ["c/legacy.md"]
    assert all("native_resources" not in call.args[0] for call in f.conn.fetch.call_args_list)


async def test_a_native_delete_collects_its_event_only_in_the_callers_transaction():
    """`events` defers the delete's event to the caller's transaction. Without
    one the delete would commit in a transaction of its own while the event
    waited in the caller's buffer, so the combination is refused before any
    database work."""
    from app.exceptions import ValidationError
    from app.repositories.events_repo import EventBuffer
    from app.services.native_revision_service import NativeRevisionService

    class _NoPool:
        def acquire(self):
            raise AssertionError("refused before any database work")

    events = EventBuffer()
    with pytest.raises(ValidationError, match="caller's transaction"):
        await NativeRevisionService(_NoPool(), payload_store=object()).delete_resource(
            namespace_id=uuid.uuid4(), surface="document", path="c/native.md", actor="actor",
            mutation_id=uuid.uuid4(), events=events,
        )
    assert not events
