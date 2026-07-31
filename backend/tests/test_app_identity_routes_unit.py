"""HTTP ownership checks for the app credential administration surface."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.routes import app_identity
from app.services.auth_service import AuthenticatedUser


def _user(*, is_admin: bool) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="operator",
        email="operator@example.com",
        display_name=None,
        is_admin=is_admin,
        auth_method="jwt",
    )


def _client(user: AuthenticatedUser) -> TestClient:
    app = FastAPI()
    app.include_router(app_identity.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_non_admin_cannot_probe_credential_metadata(monkeypatch):
    called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(app_identity, "list_app_credentials", forbidden)
    response = _client(_user(is_admin=False)).get(
        f"/api/v1/apps/{uuid.uuid4()}/credentials"
    )

    assert response.status_code == 403
    assert called is False
    assert "credentials" not in response.json()


def test_admin_issue_response_preserves_one_time_secret_contract(monkeypatch):
    app_id = uuid.uuid4()
    marker = "one-time-fixture-value"

    async def fake_issue(requested_app_id, deployment, **_kwargs):
        assert requested_app_id == app_id
        assert deployment == "production"
        return {
            "credential_id": str(uuid.uuid4()),
            "app_id": str(app_id),
            "deployment": deployment,
            "prefix": "non-secret",
            "status": "active",
            "generation": 1,
            "credential": marker,
        }

    monkeypatch.setattr(app_identity, "issue_app_credential", fake_issue)
    response = _client(_user(is_admin=True)).post(
        f"/api/v1/apps/{app_id}/credentials",
        json={"deployment": "production"},
    )

    assert response.status_code == 200
    assert response.json()["credential"] == marker
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
