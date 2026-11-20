"""Public input, authenticated identity and unsupported-backend boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from pydantic import ValidationError as ModelValidationError

from app.config import settings
from app.exceptions import ConflictError, ForbiddenError, ValidationError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.services.document_idempotency import DocumentIdempotency, DocumentIdempotencyUnsupportedError
from app.services.document_service import DocumentService
from app.util.errors import exception_envelope
from mcp_server.tools import TOOLS, available_tools


@pytest.mark.parametrize("backend,supported", [("postgres_native", True), ("native_ledger_m1", True), ("bare_git", False), ("bare_git_current", False)])
def test_catalog_advertises_explicit_support_without_mutating_validation(monkeypatch, backend, supported):
    monkeypatch.setattr(settings, "document_revision_backend", backend)
    catalog = {tool.name: tool for tool in available_tools()}
    for name in ("akb_put", "akb_update"):
        assert catalog[name].meta["akb/document_idempotency"]["supported"] is supported
        assert catalog[name].input_schema["properties"]["idempotency_key"]["format"] == "uuid"
        assert next(tool for tool in TOOLS if tool.name == name).meta is None


@pytest.mark.parametrize("key", ["", "not-a-uuid", 1, False, {}, []])
def test_models_reject_invalid_keys(key):
    with pytest.raises(ModelValidationError):
        DocumentPutRequest(vault="v", collection="", title="t", content="", idempotency_key=key)
    with pytest.raises(ModelValidationError):
        DocumentUpdateRequest(title="metadata", idempotency_key=key)


def test_binding_requires_trusted_identity_and_binds_operation_and_request():
    args = dict(namespace_id=uuid.uuid4(), actor_id=uuid.uuid4(), key=uuid.uuid4(), operation="document.put", request={"title": "x", "tags": []})
    bound = DocumentIdempotency.bind(**args)
    assert DocumentIdempotency.bind(**{**args, "request": {"tags": [], "title": "x"}}) == bound
    for change in ({"operation": "document.update"}, {"request": {"title": "changed"}}):
        other = DocumentIdempotency.bind(**{**args, **change})
        assert other.mutation_id == bound.mutation_id
        assert other.fingerprint != bound.fingerprint
    for change in ({"actor_id": uuid.uuid4()}, {"namespace_id": uuid.uuid4()}):
        assert DocumentIdempotency.bind(**{**args, **change}).mutation_id != bound.mutation_id
    with pytest.raises(ValidationError):
        DocumentIdempotency.bind(**{**args, "actor_id": None})


@pytest.mark.asyncio
async def test_bare_git_rejects_keys_before_any_storage_work():
    service = object.__new__(DocumentService)
    service._repos = AsyncMock(side_effect=AssertionError("must not touch storage"))
    with pytest.raises(DocumentIdempotencyUnsupportedError) as error:
        await service.put(DocumentPutRequest(vault="v", collection="", title="t", content="", idempotency_key=uuid.uuid4()))
    assert error.value.status_code == 501
    assert error.value.code == "document_idempotency_unsupported"
    assert exception_envelope(error.value)["code"] == "document_idempotency_unsupported"
    with pytest.raises(DocumentIdempotencyUnsupportedError):
        await service.update("v", "d.md", DocumentUpdateRequest(title="t", idempotency_key=uuid.uuid4()))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["put", "update"])
async def test_mcp_rechecks_current_access_and_supplies_authenticated_actor(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from mcp_server import server
    actor = uuid.uuid4()
    key = uuid.uuid4()
    access = AsyncMock()
    method = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {"kind": "document_write"}))
    monkeypatch.setattr(server, "check_vault_access", access)
    monkeypatch.setattr(server, "doc_service", SimpleNamespace(
        put=method, update=method, get=AsyncMock(return_value=SimpleNamespace(path="d.md")),
    ))
    args = {"vault": "v", "title": "title", "content": "body"} if operation == "put" else {"uri": "akb://v/doc/d.md", "title": "metadata"}
    args["idempotency_key"] = str(key)
    handler = getattr(server, f"_handle_{operation}")
    user = SimpleNamespace(username="display-name")
    await handler(args, str(actor), user)
    assert method.await_args.kwargs["idempotency_actor"] == actor
    req = method.await_args.args[0 if operation == "put" else 2]
    assert req.idempotency_key == key
    method.reset_mock()
    access.side_effect = ForbiddenError("revoked")
    with pytest.raises(ForbiddenError):
        await handler(args, str(actor), user)
    method.assert_not_awaited()
    access.reset_mock()
    error = await handler({**args, "idempotency_key": "private-invalid-key"}, str(actor), user)
    assert error["code"] == "invalid_argument"
    assert error["details"]["field"] == "/idempotency_key"
    assert "private-invalid-key" not in str(error)
    access.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_guide_owner_check_cannot_replace_writer_vault(monkeypatch, tmp_path, surface):
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from app.api.routes import documents
    from app.services.skill_policy import VAULT_SKILL_PATH
    from mcp_server import server

    handler = documents if surface == "rest" else server
    access = AsyncMock(side_effect=[{"vault_id": uuid.uuid4()}, {"vault_id": uuid.uuid4()}])
    service = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(path=VAULT_SKILL_PATH)), update=AsyncMock())
    monkeypatch.setattr(handler, "check_vault_access", access)
    monkeypatch.setattr(handler, "doc_service", service)
    user = SimpleNamespace(user_id=str(uuid.uuid4()), username="owner")
    with pytest.raises(ConflictError, match="Vault identity changed"):
        if surface == "rest":
            await documents.update_document("example", VAULT_SKILL_PATH, DocumentUpdateRequest(title="Guide"), user)
        else:
            await server._handle_update({"uri": f"akb://example/doc/{VAULT_SKILL_PATH}", "title": "Guide"}, user.user_id, user)
    assert [call.kwargs["required_role"] for call in access.await_args_list] == ["writer", "owner"]
    service.update.assert_not_awaited()
