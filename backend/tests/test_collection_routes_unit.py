"""Exact REST boundary contract for collection create/delete."""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from app.config import settings

# The route module constructs DocumentService at import time. Keep this unit
# test independent of the deployment-only default `/data/vaults` path.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-collection-routes-test-")

from app.api.deps import get_current_user
from app.api.routes import collections  # noqa: E402
from app.main import app  # noqa: E402
from app.services.collection_service import CollectionNotEmptyError  # noqa: E402


class _FakeUser:
    user_id = "u-collections"
    username = "collections-tester"


@pytest.fixture
def client(monkeypatch):
    async def _writer(*_args, **_kwargs):
        return {"role": "writer"}

    monkeypatch.setattr(collections, "check_vault_access", _writer)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_create_adds_only_literal_kind_and_preserves_null_zero(client, monkeypatch):
    async def _create(**kwargs):
        assert kwargs == {
            "vault": "reef",
            "path": "guides/api",
            "summary": None,
            "agent_id": "u-collections",
        }
        return {
            "ok": True,
            "created": False,
            "collection": {
                "path": "guides/api",
                "name": "api",
                "summary": None,
                "doc_count": 0,
            },
        }

    monkeypatch.setattr(collections.collection_service, "create", _create)
    response = client.post(
        "/api/v1/collections/reef",
        json={"path": "guides/api", "summary": None},
    )

    assert response.status_code == 200
    assert response.json() == {
        "kind": "collection_create",
        "ok": True,
        "created": False,
        "collection": {
            "path": "guides/api",
            "name": "api",
            "summary": None,
            "doc_count": 0,
        },
    }


def test_delete_adds_only_literal_kind_and_preserves_all_counts(client, monkeypatch):
    async def _delete(**kwargs):
        assert kwargs == {
            "vault": "reef",
            "path": "guides/api",
            "recursive": True,
            "agent_id": "u-collections",
        }
        return {
            "ok": True,
            "collection": "guides/api",
            "deleted_docs": 2,
            "deleted_files": 0,
            "deleted_sub_collections": 1,
            "deleted_tables": 0,
        }

    monkeypatch.setattr(collections.collection_service, "delete", _delete)
    response = client.delete(
        "/api/v1/collections/reef/guides/api",
        params={"recursive": "true"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "kind": "collection_delete",
        "ok": True,
        "collection": "guides/api",
        "deleted_docs": 2,
        "deleted_files": 0,
        "deleted_sub_collections": 1,
        "deleted_tables": 0,
    }


def test_non_empty_409_keeps_legacy_detail_and_normalized_details(client, monkeypatch):
    async def _delete(**_kwargs):
        raise CollectionNotEmptyError(2, 0, 1, 0)

    monkeypatch.setattr(collections.collection_service, "delete", _delete)
    response = client.delete("/api/v1/collections/reef/guides")

    assert response.status_code == 409
    payload = response.json()
    expected_counts = {
        "doc_count": 2,
        "file_count": 0,
        "sub_collection_count": 1,
        "table_count": 0,
    }
    assert payload["details"] == expected_counts
    assert payload["detail"] == {
        "message": "Collection has 2 document(s), 1 sub-collection(s)",
        **expected_counts,
    }
