"""Compatibility-safe soft-uniqueness contract for document titles."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.exceptions import DocumentTitleConflictError
from app.models.document import (
    DocumentMoveRequest,
    DocumentPutRequest,
    DocumentUpdateRequest,
)
from app.services.document_service import DocumentService


def _put_request(**overrides) -> DocumentPutRequest:
    values = {
        "vault": "engineering",
        "collection": "notes",
        "title": "API Guide",
        "content": "body",
    }
    values.update(overrides)
    return DocumentPutRequest(**values)


def test_existing_callers_keep_lossless_allow_default():
    assert _put_request().title_conflict_policy == "allow"
    assert DocumentUpdateRequest(title="API Guide").title_conflict_policy == "allow"
    assert DocumentMoveRequest(collection="notes").title_conflict_policy == "allow"


def test_interactive_callers_can_opt_into_reject_but_not_invent_policies():
    assert _put_request(title_conflict_policy="reject").title_conflict_policy == "reject"
    with pytest.raises(PydanticValidationError):
        _put_request(title_conflict_policy="overwrite")


def test_title_conflict_exposes_machine_readable_existing_document():
    error = DocumentTitleConflictError(
        title=" API Guide ",
        collection="notes",
        existing_path="notes/api-guide.md",
        existing_title="API Guide",
    )

    assert error.status_code == 409
    assert error.code == "document_title_conflict"
    assert error.details == {
        "title": "API Guide",
        "collection": "notes",
        "existing_path": "notes/api-guide.md",
        "existing_title": "API Guide",
    }


@pytest.mark.asyncio
async def test_put_rejects_exact_title_before_path_or_git_mutation():
    class _DocumentRepo:
        async def find_title_conflict(self, *args, **kwargs):
            return {
                "path": "notes/api-guide.md",
                "title": "API Guide",
            }

    service = object.__new__(DocumentService)
    request = _put_request(title_conflict_policy="reject")

    with pytest.raises(DocumentTitleConflictError):
        await service._put_locked(
            req=request,
            agent_id="gnu",
            vault_id=SimpleNamespace(hex="vault"),
            doc_id=SimpleNamespace(hex="document"),
            base_path="notes/api-guide.md",
            base_slug="api-guide",
            explicit_slug=False,
            now=None,
            normalized_collection="notes",
            doc_repo=_DocumentRepo(),
            coll_repo=object(),
            conn=object(),
        )
