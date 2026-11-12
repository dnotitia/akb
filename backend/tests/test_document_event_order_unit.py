"""Bare Git document writes lock their rows, then the Vault's event lock, then
write to Git.

A Git commit does not roll back with the database transaction. If the Vault's
event lock were first taken by the event insert after the commit, a lock wait
that ran past its deadline would roll the transaction back and leave the
commit in Git. The writes take the lock first, so a busy Vault fails them
before Git changes, and the event insert finds the lock already held. The rows
they change after the commit (collections, edges) are locked before the event
lock, so they do not wait for another transaction while holding it.
``test_document_git_event_lock_postgres.py`` checks the outcome against a real
database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

import app.repositories.document_repo as document_repo
import app.services.document_service as ds
import app.services.publication_service as publication_service
from app.models.document import DocumentPutRequest
from app.services.document_service import DocumentService
from tests.test_document_hash_contract_unit import (
    _CommittingGit,
    _FakePutDocRepo,
    _patch_index_side_effects,
)

pytestmark = pytest.mark.asyncio

# Stands in for the `_path_lock` connection; every statement that would use it
# is replaced below.
_CONN = object()


def _recorder(calls: list[str], name: str):
    async def record(*_args, **_kwargs):
        calls.append(name)

    return record


def _collection_locker(calls: list[str]):
    async def lock(_conn, collection_ids, *, share=False):
        calls.append("lock_collection_rows")
        return {cid for cid in collection_ids if cid is not None}

    return lock


async def test_put_locks_its_collection_then_the_event_lock_before_its_git_commit(monkeypatch):
    _patch_index_side_effects(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(ds, "lock_collection_rows", _collection_locker(calls))
    monkeypatch.setattr(ds, "lock_vault_events", _recorder(calls, "lock_vault_events"))
    monkeypatch.setattr(ds, "emit_event", _recorder(calls, "emit_event"))
    monkeypatch.setattr(
        publication_service, "assert_no_orphan_publication_for_document", _recorder(calls, "publications"),
    )

    class _Git(_CommittingGit):
        def commit_file(self, **kwargs):
            calls.append("git_commit")
            return super().commit_file(**kwargs)

    class _Collections:
        async def get_or_create(self, vault_id, path, conn=None):
            return uuid.uuid4()

        increment_count = staticmethod(_recorder(calls, "increment_count"))

    await DocumentService(git=_Git("a" * 40))._put_locked(
        req=DocumentPutRequest(vault="order", collection="notes", title="Order", content="Body"),
        agent_id="tester", vault_id=uuid.uuid4(), doc_id=uuid.uuid4(),
        base_path="notes/order.md", base_slug="order", explicit_slug=True,
        now=datetime.now(timezone.utc), normalized_collection="notes",
        doc_repo=_FakePutDocRepo(), coll_repo=_Collections(), conn=_CONN,
    )

    assert calls == [
        "publications", "lock_collection_rows", "lock_vault_events", "git_commit",
        "increment_count", "emit_event",
    ]


async def test_delete_locks_its_rows_then_the_event_lock_before_its_git_delete(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ds, "lock_collection_rows", _collection_locker(calls))
    monkeypatch.setattr(ds, "lock_resource_edges", _recorder(calls, "lock_resource_edges"))
    monkeypatch.setattr(ds, "lock_vault_events", _recorder(calls, "lock_vault_events"))
    monkeypatch.setattr(ds, "emit_event", _recorder(calls, "emit_event"))
    monkeypatch.setattr(ds, "delete_document_chunks", _recorder(calls, "chunks"))
    monkeypatch.setattr(ds, "delete_document_relations", _recorder(calls, "relations"))
    monkeypatch.setattr(document_repo, "drop_aliases_for_resource", _recorder(calls, "aliases"))

    class _Git:
        def delete_file(self, **_kwargs):
            calls.append("git_delete")
            return "b" * 40

    class _Documents:
        async def delete_with_publications(self, conn, *, doc_id, vault_id):
            calls.append("delete_with_publications")
            return True

    class _Collections:
        decrement_count = staticmethod(_recorder(calls, "decrement_count"))

    await DocumentService(git=_Git())._delete_locked(
        vault="order", vault_id=uuid.uuid4(),
        row={"id": uuid.uuid4(), "path": "notes/order.md", "collection_id": uuid.uuid4()},
        agent_id="tester", doc_repo=_Documents(), coll_repo=_Collections(), conn=_CONN,
    )

    assert calls == [
        "lock_collection_rows", "lock_resource_edges", "lock_vault_events", "git_delete",
        "chunks", "relations", "aliases", "delete_with_publications", "decrement_count",
        "emit_event",
    ]
