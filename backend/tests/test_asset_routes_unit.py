"""REST boundary contract for editor image uploads."""

from __future__ import annotations

import uuid

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
