"""Lock-order regression coverage for external Git document writes.

The mirror writer participates in the same vault lifecycle as interactive
document and image writes.  It must lock the parent vault before it asks the
document repository to lock or update a child row.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import external_git_service as egs


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class _Connection:
    def __init__(self, order: list[str], vault_id: uuid.UUID):
        self.order = order
        self.vault_id = vault_id

    def transaction(self):
        return _AsyncContext()

    async def fetchval(self, sql, *args):
        assert args == (self.vault_id,)
        if "FROM vaults" in sql:
            assert "FOR KEY SHARE" in sql
            self.order.append("vault")
            return self.vault_id
        assert "FROM vault_external_git" in sql and "sync_state = 'active'" in sql
        self.order.append("sidecar")
        return 1


class _Pool:
    def __init__(self, conn: _Connection):
        self.conn = conn

    def acquire(self):
        return _AsyncContext(self.conn)


class _Git:
    def cat_blob(self, _vault_name, _blob_sha):
        return b"# Mirrored document\n\nBody"

    def last_commit_for_path(self, _vault_name, _path, tip_sha):
        return tip_sha


@pytest.mark.asyncio
async def test_external_git_write_locks_vault_before_document(monkeypatch):
    vault_id = uuid.uuid4()
    document_id = uuid.uuid4()
    order: list[str] = []
    pool = _Pool(_Connection(order, vault_id))

    class _DocumentRepository:
        def __init__(self, _pool):
            pass

        async def find_asset_sync_state_for_update(self, *_args, **_kwargs):
            order.append("document")
            return None

        async def upsert_external(self, **_kwargs):
            return document_id, True

    class _CollectionRepository:
        def __init__(self, _pool):
            pass

    async def _get_pool():
        return pool

    async def _write_chunks(*_args, **_kwargs):
        return None

    async def _emit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(egs, "get_pool", _get_pool)
    monkeypatch.setattr(egs, "DocumentRepository", _DocumentRepository)
    monkeypatch.setattr(egs, "CollectionRepository", _CollectionRepository)
    monkeypatch.setattr(egs, "write_source_chunks", _write_chunks)
    monkeypatch.setattr(egs, "emit_event", _emit)

    await egs.ExternalGitService(git=_Git())._reindex_file(
        vault_id=vault_id,
        vault_name="mirror",
        path="document.md",
        blob_sha="b" * 40,
        remote_url="https://example.com/org/repo.git",
        tip_sha="a" * 40,
    )

    assert order[:3] == ["vault", "sidecar", "document"]
