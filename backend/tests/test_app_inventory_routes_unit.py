"""HTTP ownership and cache contracts for the app inventory routes."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_app, get_current_user
from app.api.routes import app_inventory
from app.services.auth_service import AuthenticatedUser
from app.services.app_identity_service import AppPrincipal


def _user(*, is_admin: bool) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="operator",
        email="operator@example.com",
        display_name=None,
        is_admin=is_admin,
        auth_method="jwt",
    )


def _principal(app_id: uuid.UUID) -> AppPrincipal:
    return AppPrincipal(
        app_id=app_id,
        credential_id=uuid.uuid4(),
        credential_generation=1,
        deployment="test",
        token_id="token-id",
        expires_at=None,  # type: ignore[arg-type]
    )


def _client(*, user: AuthenticatedUser | None = None, principal: AppPrincipal | None = None):
    app = FastAPI()
    app.include_router(app_inventory.router, prefix="/api/v1")
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    if principal is not None:
        app.dependency_overrides[get_current_app] = lambda: principal
    return TestClient(app)


def test_non_admin_cannot_probe_inventory(monkeypatch):
    called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"items": [], "next_cursor": None}

    monkeypatch.setattr(app_inventory, "list_inventory", forbidden)
    response = _client(user=_user(is_admin=False)).get(
        f"/api/v1/apps/{uuid.uuid4()}/inventory"
    )

    assert response.status_code == 403
    assert called is False
    assert "inventory" not in response.json().get("message", "").lower()


def test_admin_inventory_is_explicitly_scoped_and_no_store(monkeypatch):
    app_id = uuid.uuid4()
    calls = []

    async def fake_list(requested_app_id, **kwargs):
        calls.append((requested_app_id, kwargs))
        return {"items": [], "next_cursor": None}

    monkeypatch.setattr(app_inventory, "list_inventory", fake_list)
    response = _client(user=_user(is_admin=True)).get(
        f"/api/v1/apps/{app_id}/inventory?limit=7&lifecycle=active"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls == [
        (
            app_id,
            {
                "limit": 7,
                "cursor": None,
                "lifecycle": "active",
                "scope": "admin",
            },
        )
    ]


def test_app_inventory_uses_token_app_id_and_cannot_select_another_app(monkeypatch):
    app_id = uuid.uuid4()
    principal = _principal(app_id)
    auth_calls = []
    list_calls = []

    async def fake_authorize(requested, **kwargs):
        auth_calls.append((requested.app_id, kwargs["capability"]))

    async def fake_list(requested_app_id, **kwargs):
        list_calls.append((requested_app_id, kwargs))
        return {"items": [], "next_cursor": None}

    monkeypatch.setattr(app_inventory, "authorize_app_capability", fake_authorize)
    monkeypatch.setattr(app_inventory, "list_inventory", fake_list)
    response = _client(principal=principal).get(
        "/api/v1/app/inventory?app_id=" + str(uuid.uuid4())
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert auth_calls == [(app_id, "inventory:read")]
    assert list_calls[0][0] == app_id
    assert list_calls[0][1]["scope"] == "app"
    assert list_calls[0][1]["capability"] == "inventory:read"


def test_snapshot_create_accepts_empty_body(monkeypatch):
    app_id = uuid.uuid4()

    async def fake_create(requested_app_id, **kwargs):
        assert requested_app_id == app_id
        assert kwargs["requested_by_kind"] == "admin"
        return {"snapshot_id": str(uuid.uuid4()), "target_count": 0}

    monkeypatch.setattr(app_inventory, "create_rollout_snapshot", fake_create)
    response = _client(user=_user(is_admin=True)).post(
        f"/api/v1/apps/{app_id}/rollout-snapshots"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
