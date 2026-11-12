"""Summary/domain blank, omitted and legacy-null contracts on both services.

Persistence I/O is in memory; the real merge, Markdown serialization, revision
write and subsequent service GET run to guard the complete metadata round-trip.
"""
import contextlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.models.document import DocumentUpdateRequest
from app.services import document_service as git_module
from app.services.document_service import DocumentService, _compose_markdown, _parse_markdown
from app.services.native_document_service import NativeDocumentService


@pytest.mark.parametrize("backend", ["git", "native"])
@pytest.mark.parametrize("operation", ["omitted", "null", "empty", "summary", "domain"])
async def test_metadata_clear_round_trip(monkeypatch, backend, operation):
    vault_id, resource_id = uuid.uuid4(), uuid.uuid4()
    timestamp = datetime.now(UTC)
    metadata = {
        "title": "Metadata contract", "type": "note", "status": "active",
        "summary": "Existing summary", "domain": "engineering", "tags": [],
        "created_at": timestamp.isoformat(), "updated_at": timestamp.isoformat(),
    }
    raw = _compose_markdown(metadata, "Original body")
    values = {"title": "Updated title"}
    if operation in {"null", "empty"}:
        values.update(summary=None if operation == "null" else "", domain=None if operation == "null" else "")
    elif operation in {"summary", "domain"}:
        values[operation] = ""
    request = DocumentUpdateRequest(**values)
    assert ("summary" in request.model_fields_set) is ("summary" in values)

    if backend == "native":
        native = SimpleNamespace(head=SimpleNamespace(
            resource_id=resource_id, revision_id="initial-revision", parent_revision_id=None,
            path="notes/metadata.md", text=raw,
            resource_created_at=timestamp, resource_updated_at=timestamp,
        ))

        async def replace_text(**kwargs):
            assert kwargs["expected_revision_id"] == native.head.revision_id
            native.head = SimpleNamespace(
                **{**vars(native.head), "text": kwargs["payload"],
                   "parent_revision_id": native.head.revision_id, "revision_id": "updated-revision"},
            )
            return native.head

        service = NativeDocumentService(pool=object())
        native.replace_text = AsyncMock(side_effect=replace_text)
        monkeypatch.setattr(service, "_native", AsyncMock(return_value=native))
        monkeypatch.setattr(service, "_current", AsyncMock(side_effect=lambda *_: (vault_id, native.head)))
        monkeypatch.setattr(service, "_public_slug", AsyncMock(return_value=None))
        monkeypatch.setattr(service, "_created_by_name", AsyncMock(return_value=None))
        await service.update("mine", "notes/metadata.md", request)
        saved_raw = native.head.text
    else:
        row = {
            "id": resource_id, "vault_name": "mine", "path": "notes/metadata.md",
            "title": metadata["title"], "doc_type": "note", "status": "active",
            "summary": metadata["summary"], "domain": metadata["domain"], "tags": [],
            "created_by": None, "created_at": timestamp, "updated_at": timestamp,
            "current_commit": "initial-commit", "content_hash": None,
            "hash_algorithm": None, "content_hash_commit": None,
        }
        storage = SimpleNamespace(raw=raw)

        def commit_file(**kwargs):
            storage.raw = kwargs["content"]
            return "updated-commit"

        git = SimpleNamespace(read_file=lambda *_: storage.raw, commit_file=commit_file)
        repository = SimpleNamespace(
            find_by_ref=AsyncMock(side_effect=lambda *_: dict(row)),
            find_by_ref_with_conn=AsyncMock(side_effect=lambda *_, **__: dict(row)),
            update_hash=AsyncMock(),
        )

        async def update_projection(_id, **kwargs):
            # Mirror the repository's existing COALESCE contract. Null keeps
            # the previous projection; explicit empty strings are persisted.
            for field in ("title", "doc_type", "status", "summary", "domain", "tags", "content_hash", "hash_algorithm", "content_hash_commit"):
                if kwargs.get(field) is not None:
                    row[field] = kwargs[field]
            row["current_commit"] = kwargs["commit_hash"]

        repository.update = AsyncMock(side_effect=update_projection)
        service = DocumentService(git=git)
        vault_repository = SimpleNamespace(get_id_by_name=AsyncMock(return_value=vault_id))
        monkeypatch.setattr(service, "_repos", AsyncMock(return_value=(vault_repository, repository, object())))
        monkeypatch.setattr(service, "_get_public_slug", AsyncMock(return_value=None))
        monkeypatch.setattr(service, "_resolve_author_name", AsyncMock(return_value=None))

        @asynccontextmanager
        async def path_lock(*_, **__):
            # Statements are faked; only the savepoints run on the connection.
            yield SimpleNamespace(transaction=contextlib.nullcontext)

        monkeypatch.setattr(service, "_path_lock", path_lock)
        monkeypatch.setattr(git_module.asset_service, "claim_document_assets", AsyncMock(return_value=set()))
        monkeypatch.setattr(git_module.asset_service, "sync_document_assets", AsyncMock())
        monkeypatch.setattr(git_module, "emit_event", AsyncMock())
        monkeypatch.setattr(git_module, "lock_collection_rows", AsyncMock(return_value=set()))
        monkeypatch.setattr(git_module, "lock_vault_events", AsyncMock())
        await service.update("mine", "notes/metadata.md", request)
        saved_raw = storage.raw

    actual = await service.get("mine", "notes/metadata.md")
    saved_metadata, saved_body = _parse_markdown(saved_raw)
    expected_summary = "" if operation in {"empty", "summary"} else metadata["summary"]
    expected_domain = "" if operation in {"empty", "domain"} else metadata["domain"]
    assert actual.summary == saved_metadata["summary"] == expected_summary
    assert actual.domain == saved_metadata["domain"] == expected_domain
    assert actual.title == "Updated title"
    assert actual.content == saved_body == "Original body"


def test_update_schema_keeps_legacy_null_and_omitted_values_distinct():
    omitted = DocumentUpdateRequest()
    legacy_null = DocumentUpdateRequest(summary=None, domain=None)
    explicit_clear = DocumentUpdateRequest(summary="", domain="")
    assert omitted.model_dump(exclude_unset=True) == {}
    assert legacy_null.model_dump(exclude_unset=True) == {"summary": None, "domain": None}
    assert explicit_clear.model_dump(exclude_unset=True) == {"summary": "", "domain": ""}
