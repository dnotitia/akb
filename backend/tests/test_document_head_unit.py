"""Document Head reads expose only live metadata and authorize before lookup."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.exceptions import NotFoundError
from app.models.document import DocumentHeadResponse
from app.repositories.native_revision_repo import NativeResourceReferenceAmbiguousError
from app.services.document_service import DocumentService
from app.services.native_document_service import NativeDocumentService
from app.services.uri_service import doc_uri


_UPDATED_AT = datetime(2026, 10, 20, 1, 2, 3, tzinfo=UTC)


def _forbidden(*args, **kwargs):
    raise AssertionError("Head reads must not read bodies, hash, write caches, or enumerate history")


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [None, "a" * 40])
async def test_legacy_head_uses_catalog_ref_resolution_without_body_or_cache(monkeypatch, commit):
    vault_id = uuid.uuid4()
    row = {
        "id": uuid.uuid4(),
        "vault_name": "reef",
        "path": "archive/moved.md",
        "current_commit": commit,
        "updated_at": _UPDATED_AT,
    }
    repository = SimpleNamespace(find_by_ref=AsyncMock(return_value=row), update_hash=_forbidden)
    service = DocumentService(git=SimpleNamespace(read_file=_forbidden, file_log=_forbidden))
    monkeypatch.setattr(service, "_repos", AsyncMock(return_value=(
        SimpleNamespace(get_id_by_name=AsyncMock(return_value=vault_id)), repository, None,
    )))
    monkeypatch.setattr(service, "_ensure_document_hash", _forbidden)
    monkeypatch.setattr(service, "_get_public_slug", _forbidden)
    monkeypatch.setattr(service, "_resolve_author_name", _forbidden)
    monkeypatch.setattr("app.services.document_service.compute_text_content_hash", _forbidden)
    monkeypatch.setattr("app.services.document_service.emit_event", _forbidden)

    head = await service.get_head("reef", "notes/old.md")

    repository.find_by_ref.assert_awaited_once_with(vault_id, "notes/old.md")
    assert head.model_dump() == {
        "kind": "document_head",
        "uri": doc_uri("reef", "archive/moved.md"),
        "vault": "reef",
        "path": "archive/moved.md",
        "current_commit": commit,
        "updated_at": _UPDATED_AT,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("vault_exists", [True, False])
async def test_legacy_head_missing_vault_or_document_is_not_found(monkeypatch, vault_exists):
    lookup = AsyncMock(return_value=None)
    service = object.__new__(DocumentService)
    monkeypatch.setattr(service, "_repos", AsyncMock(return_value=(
        SimpleNamespace(get_id_by_name=AsyncMock(return_value=uuid.uuid4() if vault_exists else None)),
        SimpleNamespace(find_by_ref=lookup), None,
    )))

    with pytest.raises(NotFoundError) as caught:
        await service.get_head("reef", "deleted.md")

    assert caught.value.status_code == 404
    assert lookup.await_count == int(vault_exists)


@pytest.mark.asyncio
async def test_native_head_uses_live_reference_without_snapshot_or_migration_mapping(monkeypatch):
    pool = object()
    vault_id = uuid.uuid4()
    service = NativeDocumentService(pool=pool)
    monkeypatch.setattr(service, "_vault_id", AsyncMock(return_value=vault_id))
    lookup = AsyncMock(return_value={
        "resource_id": uuid.uuid4(),
        "current_path": "archive/moved.md",
        "head_revision_id": "b" * 40,
        "updated_at": _UPDATED_AT,
    })
    monkeypatch.setattr(
        "app.services.native_document_service.NativeRevisionRepository",
        lambda selected_pool: SimpleNamespace(resolve_live_reference=lookup) if selected_pool is pool else None,
    )
    monkeypatch.setattr(service, "_native", _forbidden)
    monkeypatch.setattr(service, "_current", _forbidden)
    monkeypatch.setattr(service, "_response", _forbidden)
    monkeypatch.setattr("app.services.native_document_service._body_content_hash", _forbidden)
    monkeypatch.setattr("app.services.native_document_service.NativeRevisionMigrationRepository", _forbidden)
    monkeypatch.setattr("app.services.native_revision_service.emit_event", _forbidden)

    head = await service.get_head("reef", "notes/cafe\u0301.md")

    lookup.assert_awaited_once_with(
        namespace_id=vault_id, surface="document", reference="notes/café.md",
    )
    assert head.uri == doc_uri("reef", "archive/moved.md")
    assert head.current_commit == "b" * 40
    assert head.updated_at == _UPDATED_AT
    assert set(head.model_dump()) == set(DocumentHeadResponse.model_fields)


@pytest.mark.asyncio
async def test_native_head_deleted_or_unknown_ref_is_not_found(monkeypatch):
    service = NativeDocumentService(pool=object())
    monkeypatch.setattr(service, "_vault_id", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(
        "app.services.native_document_service.NativeRevisionRepository",
        lambda pool: SimpleNamespace(resolve_live_reference=AsyncMock(return_value=None)),
    )

    with pytest.raises(NotFoundError) as caught:
        await service.get_head("reef", "deleted.md")

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_native_head_preserves_reference_ambiguity(monkeypatch):
    service = NativeDocumentService(pool=object())
    reference = str(uuid.uuid4())
    monkeypatch.setattr(service, "_vault_id", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(
        "app.services.native_document_service.NativeRevisionRepository",
        lambda pool: SimpleNamespace(resolve_live_reference=AsyncMock(
            side_effect=NativeResourceReferenceAmbiguousError(reference),
        )),
    )

    with pytest.raises(NativeResourceReferenceAmbiguousError) as caught:
        await service.get_head("reef", reference)

    assert caught.value.status_code == 409
    assert caught.value.code == "native_resource_reference_ambiguous"


@pytest.mark.asyncio
async def test_native_head_matches_live_get_while_cutover_legacy_token_remains_a_write_alias(monkeypatch):
    vault_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    native_token, legacy_token = "b" * 40, "a" * 40
    service = NativeDocumentService(pool=object())
    current = SimpleNamespace(
        resource_id=resource_id, revision_id=native_token, path="notes/current.md",
        text="body", resource_created_at=_UPDATED_AT, resource_updated_at=_UPDATED_AT,
    )
    monkeypatch.setattr(service, "_current", AsyncMock(return_value=(vault_id, current)))
    monkeypatch.setattr(service, "_vault_id", AsyncMock(return_value=vault_id))
    monkeypatch.setattr(service, "_document_frontmatter", AsyncMock(return_value=({"title": "Current"}, "body")))
    monkeypatch.setattr(service, "_public_slug", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_created_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.native_document_service.NativeRevisionRepository",
        lambda pool: SimpleNamespace(resolve_live_reference=AsyncMock(return_value={
            "current_path": current.path, "head_revision_id": native_token, "updated_at": _UPDATED_AT,
        })),
    )
    mapping = AsyncMock(return_value=SimpleNamespace(legacy_git_oid=legacy_token))
    monkeypatch.setattr(
        "app.services.native_document_service.NativeRevisionMigrationRepository",
        lambda pool: SimpleNamespace(mapping_for_native_revision=mapping),
    )

    document = await service.get("reef", "notes/old.md")
    head = await service.get_head("reef", "notes/old.md")

    assert head.current_commit == document.current_commit == native_token
    mapping.assert_not_awaited()
    assert await service._expected_commit_matches_current(
        namespace_id=vault_id, resource_id=resource_id,
        expected_commit=legacy_token, current_revision_id=head.current_commit,
    )
    mapping.assert_awaited_once()


@pytest.fixture
def head_route(monkeypatch):
    from app.services import revision_backend

    service = SimpleNamespace(get_head=AsyncMock())
    monkeypatch.setattr(revision_backend, "get_document_service", lambda: service)
    from app.api.deps import get_current_user
    from app.api.routes import documents

    monkeypatch.setattr(documents, "doc_service", service)
    app = FastAPI()
    app.include_router(documents.router, prefix="/api/v1")
    return app, documents, service, get_current_user


def test_head_route_authenticates_before_access_or_lookup(head_route, monkeypatch):
    app, routes, service, _ = head_route
    access = AsyncMock(side_effect=_forbidden)
    monkeypatch.setattr(routes, "check_vault_access", access)

    response = TestClient(app).get("/api/v1/document-head/reef/notes/private.md")

    assert response.status_code == 401
    access.assert_not_awaited()
    service.get_head.assert_not_awaited()


def test_head_route_requires_read_token_scope_before_lookup(head_route, monkeypatch):
    app, routes, service, _ = head_route
    access = AsyncMock(side_effect=_forbidden)
    monkeypatch.setattr(routes, "check_vault_access", access)
    monkeypatch.setattr(
        "app.api.deps.resolve_rest_user_authorization",
        AsyncMock(return_value=SimpleNamespace(token_scopes=frozenset({"write"}))),
    )

    response = TestClient(app).get(
        "/api/v1/document-head/reef/notes/private.md", headers={"Authorization": "Bearer scoped-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"
    access.assert_not_awaited()
    service.get_head.assert_not_awaited()


def test_head_route_denies_reader_access_before_document_lookup(head_route, monkeypatch):
    app, routes, service, dependency = head_route
    app.dependency_overrides[dependency] = lambda: SimpleNamespace(user_id="reader")
    access = AsyncMock(side_effect=HTTPException(status_code=403, detail="No access"))
    monkeypatch.setattr(routes, "check_vault_access", access)

    response = TestClient(app).get("/api/v1/document-head/reef/notes/private.md")

    assert response.status_code == 403
    access.assert_awaited_once_with("reader", "reef", required_role="reader")
    service.get_head.assert_not_awaited()


def test_head_route_preserves_greedy_path_and_only_exposes_head_contract(head_route, monkeypatch):
    app, routes, service, dependency = head_route
    app.dependency_overrides[dependency] = lambda: SimpleNamespace(user_id="reader")
    authorized = Mock()

    async def access(user_id, vault, *, required_role):
        assert (user_id, vault, required_role) == ("reader", "reef", "reader")
        authorized()

    async def head(vault, reference):
        authorized.assert_called_once_with()
        assert (vault, reference) == ("reef", "notes/deep/example.md/move")
        return {
            "kind": "document_head", "uri": doc_uri("reef", "archive/current.md"),
            "vault": "reef", "path": "archive/current.md",
            "current_commit": None, "updated_at": _UPDATED_AT,
            "id": str(uuid.uuid4()), "content": "must not escape",
        }

    monkeypatch.setattr(routes, "check_vault_access", access)
    service.get_head.side_effect = head

    response = TestClient(app).get("/api/v1/document-head/reef/notes/deep/example.md/move")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "kind": "document_head", "uri": doc_uri("reef", "archive/current.md"),
        "vault": "reef", "path": "archive/current.md",
        "current_commit": None, "updated_at": "2026-10-20T01:02:03Z",
    }
    operation = app.openapi()["paths"]["/api/v1/document-head/{vault}/{doc_id}"]["get"]
    assert operation["operationId"] == "documentsGetDocumentHead"
