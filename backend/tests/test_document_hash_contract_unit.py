from __future__ import annotations

import uuid
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from app.exceptions import ConflictError
from app.models.document import BrowseItem, DocumentPutResponse, DocumentResponse, DocumentUpdateRequest
from app.services.document_service import DocumentService


class _FakeVaultRepo:
    def __init__(self, vault_id: uuid.UUID):
        self.vault_id = vault_id

    async def get_id_by_name(self, vault: str) -> uuid.UUID:
        return self.vault_id


class _FakeDocRepo:
    def __init__(self, row: dict):
        self.row = row
        self.hash_update: dict | None = None

    async def find_by_ref(self, vault_id: uuid.UUID, ref: str) -> dict:
        return dict(self.row)

    async def update_hash(
        self,
        doc_id: uuid.UUID,
        *,
        content_hash: str,
        hash_algorithm: str,
        content_hash_commit: str | None,
        conn=None,
    ) -> None:
        self.hash_update = {
            "doc_id": doc_id,
            "content_hash": content_hash,
            "hash_algorithm": hash_algorithm,
            "content_hash_commit": content_hash_commit,
        }


class _FakeGit:
    def __init__(self, raw: str):
        self.raw = raw

    def read_file(self, vault: str, path: str, commit: str | None = None) -> str:
        return self.raw

    def commit_file(self, *args, **kwargs) -> str:
        raise AssertionError("commit_file must not be called when hash precondition fails")


def test_document_response_models_expose_hash_fields() -> None:
    assert "content_hash" in DocumentResponse.model_fields
    assert "hash_algorithm" in DocumentResponse.model_fields
    assert "content_hash" in DocumentPutResponse.model_fields
    assert "hash_algorithm" in DocumentPutResponse.model_fields
    assert "current_commit" in DocumentPutResponse.model_fields
    assert "content_hash" in BrowseItem.model_fields
    assert "hash_algorithm" in BrowseItem.model_fields


@pytest.mark.asyncio
async def test_document_get_computes_body_hash_and_repairs_projection(monkeypatch) -> None:
    body = "# Body\n\nThis is the returned document body.\n"
    returned_body = body.rstrip("\n")
    raw = (
        "---\n"
        "title: Hash Contract\n"
        "updated_at: 2026-05-29T00:00:00+00:00\n"
        "---\n"
        f"{body}"
    )
    vault_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    commit = "a" * 40
    row = {
        "id": doc_id,
        "vault_name": "hash-vault",
        "path": "specs/hash-contract.md",
        "title": "Hash Contract",
        "doc_type": "spec",
        "status": "draft",
        "summary": None,
        "domain": None,
        "created_by": "tester",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "current_commit": commit,
        "content_hash": None,
        "hash_algorithm": None,
        "content_hash_commit": None,
        "tags": ["hash"],
    }
    doc_repo = _FakeDocRepo(row)
    service = DocumentService(git=_FakeGit(raw))

    async def fake_repos():
        return _FakeVaultRepo(vault_id), doc_repo, object()

    async def fake_public_slug(vault: str, path: str) -> None:
        return None

    monkeypatch.setattr(service, "_repos", fake_repos)
    monkeypatch.setattr(service, "_get_public_slug", fake_public_slug)

    result = await service.get("hash-vault", "specs/hash-contract.md")

    expected_hash = sha256(returned_body.encode("utf-8")).hexdigest()
    assert result.content == returned_body
    assert result.content_hash == expected_hash
    assert result.hash_algorithm == "sha256"
    assert result.current_commit == commit
    assert doc_repo.hash_update == {
        "doc_id": doc_id,
        "content_hash": expected_hash,
        "hash_algorithm": "sha256",
        "content_hash_commit": commit,
    }


@pytest.mark.asyncio
async def test_document_get_hashes_empty_body_as_valid_content(monkeypatch) -> None:
    raw = "---\ntitle: Empty Body\n---\n"
    vault_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    commit = "c" * 40
    row = {
        "id": doc_id,
        "vault_name": "hash-vault",
        "path": "specs/empty.md",
        "title": "Empty Body",
        "doc_type": "spec",
        "status": "draft",
        "summary": None,
        "domain": None,
        "created_by": "tester",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "current_commit": commit,
        "content_hash": None,
        "hash_algorithm": None,
        "content_hash_commit": None,
        "tags": [],
    }
    doc_repo = _FakeDocRepo(row)
    service = DocumentService(git=_FakeGit(raw))

    async def fake_repos():
        return _FakeVaultRepo(vault_id), doc_repo, object()

    async def fake_public_slug(vault: str, path: str) -> None:
        return None

    monkeypatch.setattr(service, "_repos", fake_repos)
    monkeypatch.setattr(service, "_get_public_slug", fake_public_slug)

    result = await service.get("hash-vault", "specs/empty.md")

    expected_hash = sha256(b"").hexdigest()
    assert result.content == ""
    assert result.content_hash == expected_hash
    assert doc_repo.hash_update == {
        "doc_id": doc_id,
        "content_hash": expected_hash,
        "hash_algorithm": "sha256",
        "content_hash_commit": commit,
    }


@pytest.mark.asyncio
async def test_document_update_rejects_stale_expected_content_hash() -> None:
    body = "Current body"
    raw = "---\ntitle: Hash Contract\n---\nCurrent body"
    doc_id = uuid.uuid4()
    row = {
        "id": doc_id,
        "path": "specs/hash-contract.md",
        "title": "Hash Contract",
        "doc_type": "spec",
        "summary": None,
        "current_commit": "b" * 40,
        "content_hash": None,
        "hash_algorithm": None,
        "content_hash_commit": None,
        "tags": [],
    }
    doc_repo = _FakeDocRepo(row)
    service = DocumentService(git=_FakeGit(raw))
    req = DocumentUpdateRequest(
        content="New body",
        expected_content_hash=sha256(b"stale body").hexdigest(),
    )

    with pytest.raises(ConflictError):
        await service._update_locked(
            req=req,
            agent_id="tester",
            vault="hash-vault",
            vault_id=uuid.uuid4(),
            doc_repo=doc_repo,
            row=row,
            conn=None,
        )

    assert doc_repo.hash_update == {
        "doc_id": doc_id,
        "content_hash": sha256(body.encode("utf-8")).hexdigest(),
        "hash_algorithm": "sha256",
        "content_hash_commit": row["current_commit"],
    }


# ── akb_put status option ──────────────────────────────────────────


def test_put_request_status_defaults_to_draft_and_accepts_active() -> None:
    from app.models.document import DOC_STATUSES, DocumentPutRequest

    base = dict(vault="v", collection="c", title="t", content="# x")
    assert DocumentPutRequest(**base).status == "draft"          # backward-compatible default
    assert DocumentPutRequest(**base, status="active").status == "active"
    assert set(DOC_STATUSES) == {"draft", "active", "archived", "superseded"}


@pytest.mark.asyncio
async def test_put_rejects_unknown_status() -> None:
    """An out-of-set status is rejected by DocumentService.put before any
    DB/git work (the check is the first statement in put()), so this needs
    no database."""
    from app.exceptions import ValidationError
    from app.models.document import DocumentPutRequest

    svc = DocumentService(git=_FakeGit("x"))
    req = DocumentPutRequest(vault="v", collection="c", title="t", content="# x", status="actve")
    with pytest.raises(ValidationError):
        await svc.put(req)
