"""Write intent must be explicit before either document backend reads storage."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from pydantic import ValidationError as ModelValidationError

from app.exceptions import ValidationError
from app.models.document import DocumentEditRequest, DocumentUpdateRequest
from app.services.document_service import DocumentService
from app.services.native_document_service import NativeDocumentService


@pytest.mark.parametrize("field", ["expected_commit", "expected_content_hash"])
@pytest.mark.parametrize("value", ["", " ", "x", 1, "a" * 40 + "\x00", "a" * 64 + "\x00"])
def test_invalid_update_tokens_are_not_treated_as_missing(field, value):
    with pytest.raises(ModelValidationError):
        DocumentUpdateRequest(**{field: value})


@pytest.mark.parametrize("value", ["", " ", "x", "a" * 40 + "\x00"])
def test_edit_rejects_malformed_base_token(value):
    with pytest.raises(ModelValidationError):
        DocumentEditRequest(old_string="before", new_string="after", base_commit=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["git", "native"])
@pytest.mark.parametrize("content", ["body", ""])
async def test_full_body_requires_original_read_condition_before_storage(backend, content, monkeypatch):
    service = DocumentService(git=object()) if backend == "git" else NativeDocumentService(pool=object())
    storage = AsyncMock(side_effect=AssertionError("must not read storage"))
    monkeypatch.setattr(service, "_repos" if backend == "git" else "_current", storage)
    with pytest.raises(ValidationError, match="expected_commit|expected_content_hash"):
        await service.update("vault", "note.md", DocumentUpdateRequest(content=content))
    storage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["git", "native"])
@pytest.mark.parametrize("content", [None, "body", ""])
async def test_valid_condition_and_metadata_only_reach_existing_backend(backend, content, monkeypatch):
    service = DocumentService(git=object()) if backend == "git" else NativeDocumentService(pool=object())
    storage = AsyncMock(side_effect=RuntimeError("storage boundary reached"))
    monkeypatch.setattr(service, "_repos" if backend == "git" else "_current", storage)
    values = {"content": content, "title": "title"}
    if content is not None:
        values["expected_content_hash"] = "a" * 64
    with pytest.raises(RuntimeError, match="storage boundary reached"):
        await service.update("vault", "note.md", DocumentUpdateRequest(**values))


@pytest.mark.asyncio
async def test_mcp_model_error_is_invalid_argument_without_echoing_the_body(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from mcp_server import server
    monkeypatch.setattr(server, "check_vault_access", AsyncMock())
    monkeypatch.setattr(server.doc_service, "get", AsyncMock(return_value=SimpleNamespace(path="notes/doc.md")))
    write = AsyncMock(side_effect=AssertionError("must not write"))
    monkeypatch.setattr(server.doc_service, "update", write)
    response = await server._handle_update(
        {"uri": "akb://test/coll/notes/doc/doc.md", "content": "PRIVATE BODY MARKER", "expected_commit": ""},
        "user", SimpleNamespace(username="writer"),
    )
    assert response["code"] == "invalid_argument"
    assert "PRIVATE BODY MARKER" not in str(response)
    write.assert_not_awaited()


@pytest.mark.parametrize("schema_source", ["mcp", "rest"])
def test_schema_requires_a_nonnull_condition_for_nonnull_body(schema_source):
    from jsonschema import ValidationError as SchemaError, validate
    from mcp_server.tools import TOOLS
    schema = (next(tool.input_schema for tool in TOOLS if tool.name == "akb_update")
              if schema_source == "mcp" else DocumentUpdateRequest.model_json_schema())
    common = {"uri": "akb://test/doc/doc.md"} if schema_source == "mcp" else {}
    for content in ("body", ""):
        with pytest.raises(SchemaError):
            validate({**common, "content": content}, schema)
        with pytest.raises(SchemaError):
            validate({**common, "content": content, "expected_commit": None}, schema)
        validate({**common, "content": content, "expected_commit": "a" * 40}, schema)
        validate({**common, "content": content, "expected_content_hash": "b" * 64}, schema)
    validate({**common, "content": None, "title": "metadata only"}, schema)
    for field, length in (("expected_commit", 40), ("expected_content_hash", 64)):
        with pytest.raises(SchemaError):
            validate({**common, "content": "body", field: "a" * length + "\n"}, schema)
