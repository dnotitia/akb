from __future__ import annotations

import uuid
from datetime import datetime, timezone
from hashlib import sha256
from unittest.mock import AsyncMock

import pytest

from app.exceptions import ConflictError
from app.models.document import BrowseItem, DocumentPutResponse, DocumentResponse, DocumentUpdateRequest
from app.services.document_service import DocumentService


@pytest.fixture(autouse=True)
def _stub_author_resolution(monkeypatch):
    """get()/get_at_commit() resolve created_by → display_name via a DB query
    (user_directory.resolve_display_names). These hash-contract tests mock the
    document repos but run without a DB, so stub the resolver to keep the read
    path from opening a real pool. Resolution itself is covered in
    test_user_directory_unit.py."""
    monkeypatch.setattr(
        "app.services.document_service.resolve_display_names",
        AsyncMock(return_value={}),
    )


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


class _RecordingGit:
    """Captures the `commit` argument read_file is invoked with."""

    def __init__(self, raw: str):
        self.raw = raw
        self.read_commits: list[str | None] = []

    def read_file(self, vault: str, path: str, commit: str | None = None) -> str:
        self.read_commits.append(commit)
        return self.raw


@pytest.mark.asyncio
async def test_document_get_reads_body_at_current_commit_not_head(monkeypatch) -> None:
    """E03 regression: get() must read the body at the row's current_commit,
    not the floating vault HEAD. Reading HEAD lets a concurrent writer advance
    git between the DB-row read and the git read, so a single response could
    carry a body and a current_commit from different writers. Pinning the read
    to current_commit keeps the (content, current_commit) pair consistent."""
    commit = "d" * 40
    row = {
        "id": uuid.uuid4(),
        "vault_name": "race-vault",
        "path": "race/probe.md",
        "title": "Probe",
        "doc_type": "note",
        "status": "active",
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
    git = _RecordingGit("---\ntitle: Probe\n---\nWRITER_07\n")
    service = DocumentService(git=git)

    async def fake_repos():
        return _FakeVaultRepo(uuid.uuid4()), _FakeDocRepo(row), object()

    async def fake_public_slug(vault: str, path: str) -> None:
        return None

    monkeypatch.setattr(service, "_repos", fake_repos)
    monkeypatch.setattr(service, "_get_public_slug", fake_public_slug)

    result = await service.get("race-vault", "race/probe.md")

    # The body must be read pinned to current_commit, never the floating HEAD.
    assert git.read_commits == [commit], (
        f"get() read the body at {git.read_commits!r}, expected [{commit!r}] "
        "(reading HEAD reintroduces the E03 read-side race)"
    )
    assert result.current_commit == commit


# ── write-response / get hash parity (issue #181) ─────────────────
#
# put/update used to hash the RAW request body, while get parses the
# stored markdown (frontmatter.loads strips surrounding whitespace) and
# hashes the parsed body. Any body with leading/trailing whitespace got
# a write-response hash no later read would ever confirm. The write
# path must certify the canonical parsed body — the same value get
# serves — by construction.


class _FakePutDocRepo:
    """find_by_path/create/update/update_hash fakes for the put and
    update critical sections; records what would hit the DB."""

    def __init__(self, row: dict | None = None):
        self.row = row
        self.created: dict | None = None
        self.updated: dict | None = None
        self.hash_update: dict | None = None

    async def find_by_path(self, vault_id: uuid.UUID, path: str, conn=None):
        return None

    async def find_by_ref(self, vault_id: uuid.UUID, ref: str) -> dict:
        return dict(self.row or {})

    async def create(self, **kwargs) -> uuid.UUID:
        self.created = kwargs
        return uuid.uuid4()

    async def update(self, doc_id: uuid.UUID, **kwargs) -> None:
        self.updated = {"doc_id": doc_id, **kwargs}

    async def update_hash(self, doc_id: uuid.UUID, *, content_hash, hash_algorithm, content_hash_commit, conn=None) -> None:
        self.hash_update = {
            "doc_id": doc_id,
            "content_hash": content_hash,
            "hash_algorithm": hash_algorithm,
            "content_hash_commit": content_hash_commit,
        }


class _FakeCollRepo:
    async def get_or_create(self, vault_id: uuid.UUID, path: str, conn=None) -> uuid.UUID:
        return uuid.uuid4()

    async def increment_count(self, collection_id, now, conn=None) -> None:
        return None


class _CommittingGit:
    """Records the markdown handed to commit_file and serves it back on
    read_file — i.e. behaves like the real git round-trip."""

    def __init__(self, commit: str, initial_raw: str | None = None):
        self.commit = commit
        self.initial_raw = initial_raw
        self.committed_md: str | None = None

    def commit_file(self, *, vault_name, file_path, content, message, author_name) -> str:
        self.committed_md = content
        return self.commit

    def read_file(self, vault: str, path: str, commit: str | None = None) -> str:
        if self.committed_md is not None:
            return self.committed_md
        assert self.initial_raw is not None, "read before any commit"
        return self.initial_raw


def _patch_index_side_effects(monkeypatch) -> None:
    """Neutralize the chunk/relations/event writes that need a live DB."""
    import app.repositories.document_repo as document_repo
    import app.services.document_service as ds

    async def fake_write_source_chunks(conn, kind, doc_id, *, vault_id, chunks):
        return len(chunks)

    async def fake_drop_resource_alias(*args, **kwargs):
        return None

    async def fake_store_document_relations(*args, **kwargs):
        return None

    async def fake_emit_event(*args, **kwargs):
        return None

    monkeypatch.setattr(document_repo, "drop_resource_alias", fake_drop_resource_alias)
    monkeypatch.setattr(ds, "write_source_chunks", fake_write_source_chunks)
    monkeypatch.setattr(ds, "store_document_relations", fake_store_document_relations)
    monkeypatch.setattr(ds, "emit_event", fake_emit_event)


@pytest.mark.asyncio
async def test_put_certifies_the_canonical_parsed_body_hash(monkeypatch) -> None:
    """A body with trailing newlines (Jira-style sync payloads produce
    them routinely): the put-response hash must equal the hash of the
    frontmatter round-tripped body — what a later get will serve — not
    the raw request body."""
    import frontmatter

    from app.models.document import DocumentPutRequest

    _patch_index_side_effects(monkeypatch)
    commit = "e" * 40
    git = _CommittingGit(commit)
    doc_repo = _FakePutDocRepo()
    service = DocumentService(git=git)
    req = DocumentPutRequest(
        vault="hash-vault", collection="specs", title="Trailing",
        content="Body line one.\n\nBody line two.\n\n",
    )

    resp = await service._put_locked(
        req=req, agent_id="tester", vault_id=uuid.uuid4(), doc_id=uuid.uuid4(),
        base_path="specs/trailing.md", base_slug="trailing", explicit_slug=True,
        now=datetime.now(timezone.utc), normalized_collection="specs",
        doc_repo=doc_repo, coll_repo=_FakeCollRepo(), conn=None,
    )

    # Canonical body = what frontmatter parses back out of the committed
    # markdown; this is exactly what get() hashes.
    canonical_body = frontmatter.loads(git.committed_md).content
    expected_hash = sha256(canonical_body.encode("utf-8")).hexdigest()
    assert canonical_body != req.content, "fixture must exercise the strip"
    assert resp.content_hash == expected_hash, (
        "put response certified the raw request body, not the canonical "
        "parsed body — no later akb_get will ever confirm this hash"
    )
    # The DB row gets the same canonical hash (no read-order-dependent flip).
    assert doc_repo.created is not None
    assert doc_repo.created["content_hash"] == expected_hash


@pytest.mark.asyncio
async def test_get_after_put_confirms_hash_and_self_heal_is_noop(monkeypatch) -> None:
    """End-to-end parity: get() over the row a fresh put persisted must
    return the same content_hash and never rewrite the row (the
    _ensure_document_hash self-heal is a no-op for fresh writes)."""
    from app.models.document import DocumentPutRequest

    _patch_index_side_effects(monkeypatch)
    commit = "f" * 40
    git = _CommittingGit(commit)
    put_repo = _FakePutDocRepo()
    service = DocumentService(git=git)
    req = DocumentPutRequest(
        vault="hash-vault", collection="specs", title="Trailing",
        content="# Heading\n\nBody.\n\n",
    )
    resp = await service._put_locked(
        req=req, agent_id="tester", vault_id=uuid.uuid4(), doc_id=uuid.uuid4(),
        base_path="specs/trailing.md", base_slug="trailing", explicit_slug=True,
        now=datetime.now(timezone.utc), normalized_collection="specs",
        doc_repo=put_repo, coll_repo=_FakeCollRepo(), conn=None,
    )

    # Project the row exactly as the put persisted it.
    row = {
        "id": uuid.uuid4(),
        "vault_name": "hash-vault",
        "path": "specs/trailing.md",
        "title": "Trailing",
        "doc_type": "note",
        "status": "draft",
        "summary": None,
        "domain": None,
        "created_by": "tester",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "current_commit": commit,
        "content_hash": put_repo.created["content_hash"],
        "hash_algorithm": "sha256",
        "content_hash_commit": commit,
        "tags": [],
    }
    get_repo = _FakeDocRepo(row)

    async def fake_repos():
        return _FakeVaultRepo(uuid.uuid4()), get_repo, object()

    async def fake_public_slug(vault: str, path: str) -> None:
        return None

    monkeypatch.setattr(service, "_repos", fake_repos)
    monkeypatch.setattr(service, "_get_public_slug", fake_public_slug)

    result = await service.get("hash-vault", "specs/trailing.md")

    assert result.content_hash == resp.content_hash, (
        "get() returned a different hash than the put response certified"
    )
    assert get_repo.hash_update is None, (
        "get() rewrote the row hash on first read — the write certified "
        "a non-canonical value"
    )


@pytest.mark.asyncio
async def test_update_certifies_the_canonical_parsed_body_hash(monkeypatch) -> None:
    """Same parity contract on the update path: req.content with trailing
    newlines must yield a response/DB hash over the canonical parsed body."""
    import frontmatter

    _patch_index_side_effects(monkeypatch)
    current_body = "Old body."
    current_hash = sha256(current_body.encode("utf-8")).hexdigest()
    commit = "9" * 40
    doc_id = uuid.uuid4()
    row = {
        "id": doc_id,
        "path": "specs/trailing.md",
        "title": "Trailing",
        "doc_type": "note",
        "status": "draft",
        "summary": None,
        "current_commit": "8" * 40,
        "content_hash": current_hash,
        "hash_algorithm": "sha256",
        "content_hash_commit": "8" * 40,
        "tags": [],
    }
    git = _CommittingGit(commit)
    git.committed_md = f"---\ntitle: Trailing\n---\n{current_body}"
    doc_repo = _FakePutDocRepo(row)
    service = DocumentService(git=git)
    req = DocumentUpdateRequest(content="New body.\n\n")

    resp = await service._update_locked(
        req=req, agent_id="tester", vault="hash-vault",
        vault_id=uuid.uuid4(), doc_repo=doc_repo, row=row, conn=None,
    )

    canonical_body = frontmatter.loads(git.committed_md).content
    expected_hash = sha256(canonical_body.encode("utf-8")).hexdigest()
    assert canonical_body == "New body."
    assert resp.content_hash == expected_hash, (
        "update response certified the raw request body, not the "
        "canonical parsed body"
    )
    assert doc_repo.updated is not None
    assert doc_repo.updated["content_hash"] == expected_hash
    assert resp.previous_content_hash == current_hash


@pytest.mark.asyncio
async def test_edit_certifies_the_canonical_parsed_body_hash(monkeypatch) -> None:
    """akb_edit must use the same canonical write hash as put/update.

    Replacing with a string that ends in blank lines exercises the exact
    frontmatter round-trip that strips body edges before akb_get hashes it.
    """
    import frontmatter

    _patch_index_side_effects(monkeypatch)
    current_body = "Old body."
    current_hash = sha256(current_body.encode("utf-8")).hexdigest()
    commit = "7" * 40
    doc_id = uuid.uuid4()
    row = {
        "id": doc_id,
        "path": "specs/edit.md",
        "title": "Edit Hash",
        "doc_type": "note",
        "summary": None,
        "current_commit": "6" * 40,
        "content_hash": current_hash,
        "hash_algorithm": "sha256",
        "content_hash_commit": "6" * 40,
        "tags": [],
    }
    git = _CommittingGit(
        commit,
        initial_raw=f"---\ntitle: Edit Hash\ntype: note\n---\n{current_body}",
    )
    doc_repo = _FakePutDocRepo(row)
    service = DocumentService(git=git)

    resp = await service._edit_locked(
        vault="hash-vault",
        vault_id=uuid.uuid4(),
        row=row,
        doc_repo=doc_repo,
        old_string=current_body,
        new_string="New body.\n\n",
        replace_all=False,
        message="edit hash",
        agent_id="tester",
        conn=None,
    )

    canonical_body = frontmatter.loads(git.committed_md).content
    expected_hash = sha256(canonical_body.encode("utf-8")).hexdigest()
    assert canonical_body == "New body."
    assert resp.content_hash == expected_hash, (
        "edit response certified the raw edited body, not the canonical parsed body"
    )
    assert doc_repo.updated is not None
    assert doc_repo.updated["content_hash"] == expected_hash
    assert resp.previous_content_hash == current_hash


# ── akb_put status option ──────────────────────────────────────────


def test_put_request_status_defaults_to_draft_and_accepts_active() -> None:
    from app.models.document import DOC_STATUSES, DocumentPutRequest

    base = dict(vault="v", collection="c", title="t", content="# x")
    assert DocumentPutRequest(**base).status == "draft"          # backward-compatible default
    assert DocumentPutRequest(**base, status="active").status == "active"
    assert set(DOC_STATUSES) == {"draft", "active", "archived"}


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
