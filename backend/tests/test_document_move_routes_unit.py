"""REST move route and process-selected document-service boundary tests."""

from __future__ import annotations

import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings

settings.git_storage_path = tempfile.mkdtemp(prefix="akb-document-move-route-")

from app.api.deps import get_current_user  # noqa: E402
from app.api.routes import documents  # noqa: E402
from app.models.document import DocumentPutResponse  # noqa: E402


class _User:
    user_id = "move-route-user"
    username = "move-route-agent"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(documents.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _User()
    return app


def test_post_move_route_gates_writer_and_preserves_greedy_document_path(monkeypatch):
    access_calls: list[tuple[str, str, str]] = []
    move_calls: list[tuple[str, str, dict]] = []

    async def _check_vault_access(user_id: str, vault: str, *, required_role: str):
        access_calls.append((user_id, vault, required_role))

    async def _move(vault: str, doc_ref: str, **kwargs):
        move_calls.append((vault, doc_ref, kwargs))
        return DocumentPutResponse(
            uri=f"akb://{vault}/coll/archive/doc/moved.md",
            vault=vault,
            path="archive/moved.md",
            commit_hash="a" * 40,
            current_commit="a" * 40,
            action="moved",
            chunks_indexed=0,
            entities_found=0,
        )

    monkeypatch.setattr(documents, "check_vault_access", _check_vault_access)
    monkeypatch.setattr(documents.doc_service, "move", _move)

    response = TestClient(_app()).post(
        "/api/v1/documents/reef/guides/deep/source.md/move",
        json={"collection": "archive", "slug": "moved", "message": "archive it"},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "moved"
    assert access_calls == [("move-route-user", "reef", "writer")]
    assert move_calls == [
        (
            "reef",
            "guides/deep/source.md",
            {
                "collection": "archive",
                "slug": "moved",
                "message": "archive it",
                "agent_id": "move-route-agent",
            },
        )
    ]
