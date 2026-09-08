"""Archive discovery is authoritative metadata filtering, not a new ACL."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.models.document import BrowseItem
from app.services.document_service import DocumentService
from app.services import native_document_service as native


@pytest.mark.parametrize("native_backend", [False, True])
@pytest.mark.parametrize("scope,statuses", [
    ("unarchived", ["draft", "active"]),
    ("archived", ["archived"]),
    ("all", ["draft", "active", "archived"]),
])
async def test_browse_archive_scope_retains_navigation_and_filters_before_pagination(monkeypatch, native_backend, scope, statuses):
    vault_id = uuid.UUID(int=1)
    vault_repo = SimpleNamespace(get_by_name=AsyncMock(return_value={"id": vault_id}))
    coll_repo = SimpleNamespace(list_by_vault=AsyncMock(return_value=[{
        "path": "notes", "name": "notes", "summary": None, "doc_count": 3, "last_updated": None,
    }]))
    docs = [BrowseItem(name=status, path=f"notes/{status}.md", type="document", status=status,
                       uri=f"akb://mine/coll/notes/doc/{status}.md") for status in ["draft", "active", "archived"]]
    files = [BrowseItem(name="file", path="file", type="file", uri="akb://mine/file/f")]
    tables = [BrowseItem(name="table", path="table", type="table", uri="akb://mine/table/t")]
    if native_backend:
        service = native.NativeDocumentService(pool=object())
        monkeypatch.setattr(native, "VaultRepository", lambda _: vault_repo)
        monkeypatch.setattr(native, "CollectionRepository", lambda _: coll_repo)
        doc_list, file_list, table_list = "_browse_native_documents", "_browse_legacy_files", "_browse_legacy_tables"
    else:
        service = DocumentService(git=object())
        monkeypatch.setattr(service, "_repos", AsyncMock(return_value=(vault_repo, object(), coll_repo)))
        doc_list, file_list, table_list = "_browse_docs", "_browse_files_by_depth", "_browse_tables_by_depth"
    monkeypatch.setattr(service, doc_list, AsyncMock(return_value=docs))
    monkeypatch.setattr(service, file_list, AsyncMock(return_value=files))
    monkeypatch.setattr(service, table_list, AsyncMock(return_value=tables))
    response = await service.browse("mine", depth=-1, archive_scope=scope, include_archived=scope == "unarchived")
    assert response.archive_scope == scope
    assert [item.status for item in response.items if item.type == "document"] == statuses
    assert any(item.type == "collection" for item in response.items)
    assert any(item.type == "file" for item in response.items) is (scope != "archived")
    assert any(item.type == "table" for item in response.items) is (scope != "archived")
    assert getattr(service, doc_list).call_args.kwargs["include_archived"] is (scope != "unarchived")


async def test_browse_empty_result_echoes_scope(monkeypatch):
    service = DocumentService(git=object())
    vault_repo = SimpleNamespace(get_by_name=AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_repos", AsyncMock(return_value=(vault_repo, object(), object())))
    response = await service.browse("missing", archive_scope="archived")
    assert response.items == []
    assert response.archive_scope == "archived"


@pytest.mark.parametrize("can_write", [False, True])
async def test_restore_uses_existing_writer_gate_and_patch(monkeypatch, can_write, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from app.api.routes import documents
    from app.exceptions import ForbiddenError
    from app.models.document import DocumentUpdateRequest

    access = AsyncMock(side_effect=None if can_write else ForbiddenError("Read only"))
    update = AsyncMock(return_value=object())
    monkeypatch.setattr(documents, "check_vault_access", access)
    monkeypatch.setattr(documents.doc_service, "get", AsyncMock(return_value=SimpleNamespace(path="notes/old.md")))
    monkeypatch.setattr(documents.doc_service, "update", update)
    request = DocumentUpdateRequest(status="active", expected_commit="head")
    user = SimpleNamespace(user_id="reader-or-writer", username="tester")
    if can_write:
        await documents.update_document("mine", "notes/old.md", request, user)
        assert update.call_args.args[2].status == "active"
        assert update.call_args.args[2].expected_commit == "head"
    else:
        with pytest.raises(ForbiddenError):
            await documents.update_document("mine", "notes/old.md", request, user)
        update.assert_not_awaited()
    access.assert_awaited_once_with(user.user_id, "mine", required_role="writer")
