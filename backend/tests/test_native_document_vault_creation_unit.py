"""Focused native-ledger vault creation contract."""

from __future__ import annotations

import uuid

import pytest

from app.services import native_document_service as native_documents
from app.services.native_document_service import NativeDocumentService


@pytest.mark.asyncio
async def test_create_vault_uses_postgres_repository_and_rbac_without_git(monkeypatch):
    vault_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    calls: list[tuple] = []

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class _Connection:
        def transaction(self):
            return _Transaction()

    connection = _Connection()

    class _Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    class _VaultRepository:
        def __init__(self, pool) -> None:
            assert pool is pool_marker

        async def get_by_name(self, name: str):
            calls.append(("get_by_name", name))
            return None

        async def create(self, **kwargs):
            assert kwargs.pop("conn") is connection
            calls.append(("create", kwargs))
            return vault_id

    class _RoleSync:
        async def on_vault_create_in_conn(self, conn, created_vault_id, created_owner_id) -> None:
            assert conn is connection
            calls.append(("on_vault_create", created_vault_id, created_owner_id))

        async def on_public_access_change_in_conn(self, conn, created_vault_id, public_access) -> None:
            assert conn is connection
            calls.append(("on_public_access_change", created_vault_id, public_access))

    pool_marker = _Pool()
    monkeypatch.setattr(native_documents, "VaultRepository", _VaultRepository, raising=False)
    monkeypatch.setattr(native_documents, "get_role_sync", lambda: _RoleSync(), raising=False)

    async def _seed(_service, conn, *, vault_id, vault_name, owner_id) -> None:
        assert conn is connection
        calls.append(("seed", vault_id, vault_name, owner_id))

    monkeypatch.setattr(NativeDocumentService, "_seed_native_vault_skill", _seed)

    result = await NativeDocumentService(pool=pool_marker).create_vault(
        "native-create",
        "native description",
        owner_id=str(owner_id),
        public_access="reader",
    )

    assert result == str(vault_id)
    assert calls == [
        ("get_by_name", "native-create"),
        (
            "create",
            {
                "name": "native-create",
                "description": "native description",
                "git_path": "native-ledger://native-create",
                "owner_id": owner_id,
                "public_access": "reader",
            },
        ),
        ("on_vault_create", vault_id, owner_id),
        ("on_public_access_change", vault_id, "reader"),
        ("seed", vault_id, "native-create", owner_id),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs", [{"template": "starter"}, {"external_git": {"url": "https://example.test/repo.git"}}]
)
async def test_create_vault_rejects_non_native_vault_surfaces(kwargs):
    with pytest.raises(native_documents.NativeRevisionUnsupportedSurfaceError):
        await NativeDocumentService(pool=object()).create_vault("native-create", **kwargs)
