import uuid

import pytest

from app.exceptions import NotFoundError
from app.services.document_service import DocumentService


class _VaultRepo:
    def __init__(self, vault_id):
        self.vault_id = vault_id

    async def get_id_by_name(self, _vault):
        return self.vault_id


class _DocumentRepo:
    def __init__(self, row):
        self.row = row

    async def find_by_ref(self, _vault_id, _doc_ref):
        return self.row


class _MissingGit:
    def read_file(self, _vault, _path, _commit=None):
        return None


@pytest.mark.asyncio
async def test_get_returns_not_found_when_document_row_outlives_git_blob(monkeypatch):
    vault_id = uuid.uuid4()
    row = {
        "id": uuid.uuid4(),
        "vault_name": "race-vault",
        "path": "atomic-delete/delete-race.md",
        # get() pins the body read to the row's recorded commit (E03);
        # None exercises the legacy fallback-to-HEAD path in read_file.
        "current_commit": None,
    }
    service = DocumentService(git=_MissingGit())

    async def fake_repos():
        return _VaultRepo(vault_id), _DocumentRepo(row), None

    monkeypatch.setattr(service, "_repos", fake_repos)

    with pytest.raises(NotFoundError) as exc_info:
        await service.get("race-vault", "atomic-delete/delete-race.md")

    assert exc_info.value.status_code == 404
