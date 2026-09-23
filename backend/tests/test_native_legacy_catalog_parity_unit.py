"""The projections a native write has to keep in step with the legacy arm.

The native ledger writes no legacy `documents` row, which is by design. What is
not by design is the two surfaces that still read legacy rows going blank for
native documents: the `collections` catalog that browse renders folders from,
and the `edges` rows a move has to carry to the new URI.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.services import native_document_service as native_documents
from app.services.native_document_service import NativeDocumentService

VAULT = "measure"
VAULT_ID = uuid.uuid4()


class _Connection:
    def __init__(self, *, head_path: str | None = None) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.head_path = head_path

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None

        return _tx()

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"

    async def fetchval(self, _query, *_args):
        return self.head_path


def _pool(conn):
    class _Pool:
        def acquire(self):
            @asynccontextmanager
            async def _acquire():
                yield conn

            return _acquire()

    return _Pool()


# ── collections catalog ──────────────────────────────────────


@pytest.fixture
def _recorded_collections(monkeypatch):
    created: list[tuple[uuid.UUID, str]] = []

    class _CollectionRepository:
        def __init__(self, _pool) -> None:
            pass

        async def get_or_create(self, vault_id, path, conn=None):
            created.append((vault_id, path))
            return uuid.uuid4()

    monkeypatch.setattr(native_documents, "CollectionRepository", _CollectionRepository)
    return created


async def test_a_native_write_registers_its_collection(_recorded_collections):
    service = NativeDocumentService(pool=_pool(_Connection()))

    await service._register_collection(VAULT_ID, "direction/skh-project/architecture/a.md")

    assert _recorded_collections == [(VAULT_ID, "direction/skh-project/architecture")]


async def test_a_vault_root_document_registers_nothing(_recorded_collections):
    service = NativeDocumentService(pool=_pool(_Connection()))

    await service._register_collection(VAULT_ID, "a.md")

    assert _recorded_collections == []


async def test_a_catalog_failure_does_not_fail_the_committed_write(monkeypatch, caplog):
    class _FailingRepository:
        def __init__(self, _pool) -> None:
            pass

        async def get_or_create(self, _vault_id, _path, conn=None):
            raise RuntimeError("collection row could not be written")

    monkeypatch.setattr(native_documents, "CollectionRepository", _FailingRepository)
    service = NativeDocumentService(pool=_pool(_Connection()))

    with caplog.at_level("WARNING", logger="akb.native_documents"):
        await service._register_collection(VAULT_ID, "specs/a.md")

    assert "collection catalog row not written" in caplog.text


# ── edges across a move ──────────────────────────────────────


@pytest.fixture
def _recorded_sync(monkeypatch):
    synced: list[tuple[uuid.UUID, str, str | None]] = []

    async def _sync(_conn, _vault_id, _vault_name, resource_id, path, *, adopt_path=None):
        synced.append((resource_id, path, adopt_path))

    monkeypatch.setattr(native_documents, "sync_native_document_edge_uris", _sync)
    return synced


async def test_a_native_move_carries_the_documents_edges(_recorded_sync):
    resource_id = uuid.uuid4()
    service = NativeDocumentService(pool=_pool(_Connection(head_path="direction/skh/a.md")))

    await service._relink_moved_edges(
        VAULT_ID, VAULT, old_path="direction/pipeline/a.md", new_path="direction/skh/a.md",
        resource_id=resource_id,
    )

    assert _recorded_sync == [(resource_id, "direction/skh/a.md", "direction/pipeline/a.md")]


async def test_a_move_hook_writes_the_head_path_not_the_one_it_was_handed(_recorded_sync):
    """A hook that finished late must not undo a later move (akb#655).

    The endpoint written is the resource's CURRENT head path, read under its
    row lock — never the ``new_path`` of the transition this hook belongs to.
    Here that transition ended at `b.md` while the head has already moved on
    to `c.md`, and `c.md` is what the graph gets.
    """
    resource_id = uuid.uuid4()
    service = NativeDocumentService(pool=_pool(_Connection(head_path="c.md")))

    await service._relink_moved_edges(
        VAULT_ID, VAULT, old_path="a.md", new_path="b.md", resource_id=resource_id,
    )

    assert _recorded_sync == [(resource_id, "c.md", "a.md")]


async def test_a_move_hook_for_a_deleted_resource_touches_no_edge(_recorded_sync):
    """Deletion owns its own cleanup; repointing would recreate what it drops."""
    service = NativeDocumentService(pool=_pool(_Connection(head_path=None)))

    await service._relink_moved_edges(
        VAULT_ID, VAULT, old_path="a.md", new_path="b.md", resource_id=uuid.uuid4(),
    )

    assert _recorded_sync == []


async def test_a_move_that_changes_nothing_touches_no_edge(_recorded_sync):
    service = NativeDocumentService(pool=_pool(_Connection(head_path="specs/a.md")))

    await service._relink_moved_edges(
        VAULT_ID, VAULT, old_path="specs/a.md", new_path="specs/a.md",
        resource_id=uuid.uuid4(),
    )

    assert _recorded_sync == []
