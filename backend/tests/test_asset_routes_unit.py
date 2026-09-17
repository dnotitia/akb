"""REST boundary contract for editor image uploads."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.routes import assets


class _FakeUser:
    user_id = "u-asset-upload"
    username = "asset-uploader"


def _client() -> TestClient:
    application = FastAPI()
    application.include_router(assets.router, prefix="/api/v1")
    application.dependency_overrides[get_current_user] = lambda: _FakeUser()
    return TestClient(application)


def test_editor_image_upload_preserves_raw_bytes_and_mime(monkeypatch) -> None:
    vault_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def _write_context(_request, vault, user):
        assert vault == "team"
        assert user.username == "asset-uploader"
        return {"vault_id": vault_id, "role": "writer"}, user.username, None

    async def _create_image_asset(**kwargs):
        captured.update(kwargs)
        return {
            "id": str(uuid.uuid4()),
            "url": "/api/assets/image-id",
            "name": kwargs["filename"],
            "mime_type": kwargs["declared_mime"],
            "size_bytes": len(kwargs["body"]),
        }

    monkeypatch.setattr(assets, "resolve_file_write_context", _write_context)
    monkeypatch.setattr(assets.asset_service, "create_image_asset", _create_image_asset)

    image = b"real-image-payload"
    response = _client().post(
        "/api/v1/assets/team?filename=diagram.png",
        content=image,
        headers={"Content-Type": "image/png"},
    )

    assert response.status_code == 201
    assert captured == {
        "vault_id": vault_id,
        "vault_name": "team",
        "filename": "diagram.png",
        "declared_mime": "image/png",
        "body": image,
        "actor_id": "asset-uploader",
    }
    assert response.json()["size_bytes"] == len(image)


def test_file_copy_route_uses_writer_scope_and_returns_attachment(monkeypatch) -> None:
    vault_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def _write_context(_request, vault, user):
        assert vault == "team"
        assert user.username == "asset-uploader"
        return {"vault_id": vault_id, "role": "writer"}, user.username, None

    async def _copy(**kwargs):
        captured.update(kwargs)
        return {
            "kind": "attachment",
            "id": "attachment-id",
            "target": "/api/assets/attachment-id",
            "url": "/api/assets/attachment-id",
        }

    monkeypatch.setattr(assets, "resolve_file_write_context", _write_context)
    monkeypatch.setattr(assets.asset_service, "copy_file_to_attachment", _copy)

    response = _client().post(
        "/api/v1/assets/team/from-file/11111111-1111-4111-8111-111111111111",
    )

    assert response.status_code == 201
    assert captured == {
        "vault_id": vault_id,
        "vault_name": "team",
        "file_id": "11111111-1111-4111-8111-111111111111",
        "actor_id": "asset-uploader",
    }


def test_attachment_policy_is_reader_scoped(monkeypatch) -> None:
    vault_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def _access(user_id, vault, required_role):
        captured.update(user_id=user_id, vault=vault, required_role=required_role)
        return {"vault_id": vault_id, "role": "reader"}

    monkeypatch.setattr(assets, "check_vault_access", _access)
    response = _client().get("/api/v1/assets/team/policy")

    assert response.status_code == 200
    assert response.json()["kind"] == "attachment_policy"
    assert response.json()["vault"] == "team"
    assert response.json()["unclaimed_ttl_hours"] >= 1
    assert captured == {
        "user_id": "u-asset-upload",
        "vault": "team",
        "required_role": "reader",
    }


def test_attachment_metadata_uses_authorized_reachability_and_computes_expiry(monkeypatch) -> None:
    file_id = uuid.UUID("11111111-2222-4333-8444-555555555555")
    created_at = datetime(2026, 9, 9, tzinfo=timezone.utc)

    class _Acquire:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def _pool():
        return _Pool()

    async def _find(_conn, **kwargs):
        assert kwargs["file_id"] == file_id
        assert kwargs["vault_id"] == vault_id
        return {
            "id": file_id,
            "created_at": created_at,
            "attachment_claimed_at": None,
        }

    async def _access(_user_id, _vault, required_role):
        assert required_role == "reader"
        return {"vault_id": vault_id}

    vault_id = uuid.uuid4()
    monkeypatch.setattr(assets, "get_pool", _pool)
    monkeypatch.setattr(assets, "check_vault_access", _access)
    monkeypatch.setattr(assets.vault_files_repo, "find_authorized_attachment", _find)

    response = _client().get(f"/api/v1/assets/team/{file_id}/metadata")

    assert response.status_code == 200
    assert response.json() == {
        "kind": "attachment",
        "target": f"/api/assets/{file_id}",
        "status": "unclaimed",
        "unclaimed_expires_at": "2026-09-10T00:00:00+00:00",
    }
