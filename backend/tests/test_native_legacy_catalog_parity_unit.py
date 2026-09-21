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
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None

        return _tx()

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"


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
def _recorded_relink(monkeypatch):
    relinked: list[tuple[str, str]] = []

    async def _relink(_conn, _vault_id, old_uri, new_uri):
        relinked.append((old_uri, new_uri))

    monkeypatch.setattr(native_documents, "relink_resource_edges", _relink)
    return relinked


async def test_a_native_move_carries_the_documents_edges(_recorded_relink):
    service = NativeDocumentService(pool=_pool(_Connection()))

    await service._relink_moved_edges(
        VAULT_ID, VAULT, old_path="direction/pipeline/a.md", new_path="direction/skh/a.md",
    )

    assert _recorded_relink == [
        (
            f"akb://{VAULT}/coll/direction/pipeline/doc/a.md",
            f"akb://{VAULT}/coll/direction/skh/doc/a.md",
        )
    ]


async def test_a_move_that_changes_nothing_touches_no_edge(_recorded_relink):
    service = NativeDocumentService(pool=_pool(_Connection()))

    await service._relink_moved_edges(
        VAULT_ID, VAULT, old_path="specs/a.md", new_path="specs/a.md",
    )

    assert _recorded_relink == []
