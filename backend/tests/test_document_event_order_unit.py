"""Document writes insert their event as the transaction's last locking statement.

An event insert holds its Vault's event lock until commit (migration 132). A put
that inserted its event before updating the collection count, or a delete that
did so before removing the row and decrementing the count, could wait for a
collection row held by another writer of the Vault that was itself waiting for
the event lock.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

import app.repositories.document_repo as document_repo
import app.services.document_service as ds
from app.models.document import DocumentPutRequest
from app.services.document_service import DocumentService
from tests.test_document_hash_contract_unit import (
    _CommittingGit,
    _FakePutDocRepo,
    _patch_index_side_effects,
)

pytestmark = pytest.mark.asyncio


def _recorder(calls: list[str], name: str):
    async def record(*_args, **_kwargs):
        calls.append(name)

    return record


async def test_put_inserts_its_event_after_the_collection_count(monkeypatch):
    _patch_index_side_effects(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(ds, "emit_event", _recorder(calls, "emit_event"))

    class _Collections:
        async def get_or_create(self, vault_id, path, conn=None):
            return uuid.uuid4()

        increment_count = staticmethod(_recorder(calls, "increment_count"))

    await DocumentService(git=_CommittingGit("a" * 40))._put_locked(
        req=DocumentPutRequest(vault="order", collection="notes", title="Order", content="Body"),
        agent_id="tester", vault_id=uuid.uuid4(), doc_id=uuid.uuid4(),
        base_path="notes/order.md", base_slug="order", explicit_slug=True,
        now=datetime.now(timezone.utc), normalized_collection="notes",
        doc_repo=_FakePutDocRepo(), coll_repo=_Collections(), conn=None,
    )

    assert calls == ["increment_count", "emit_event"]


async def test_delete_inserts_its_event_after_the_row_and_the_collection_count(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ds, "emit_event", _recorder(calls, "emit_event"))
    monkeypatch.setattr(ds, "delete_document_chunks", _recorder(calls, "chunks"))
    monkeypatch.setattr(ds, "delete_document_relations", _recorder(calls, "relations"))
    monkeypatch.setattr(document_repo, "drop_aliases_for_resource", _recorder(calls, "aliases"))

    class _Git:
        def delete_file(self, **_kwargs):
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
        agent_id="tester", doc_repo=_Documents(), coll_repo=_Collections(), conn=None,
    )

    assert calls[-3:] == ["delete_with_publications", "decrement_count", "emit_event"]
