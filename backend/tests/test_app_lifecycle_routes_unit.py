"""HTTP ownership, status, and cache contracts for lifecycle routes."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_app, get_current_user
from app.api.routes import app_lifecycle
from app.exceptions import AKBError, ForbiddenError
from app.main import akb_error_handler
from app.services.app_identity_service import AppPrincipal
from app.services.auth_service import AuthenticatedUser


def _user(*, is_admin: bool = True) -> AuthenticatedUser:
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


def _client(
    *,
    user: AuthenticatedUser | None = None,
    principal: AppPrincipal | None = None,
) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(AKBError, akb_error_handler)
    app.include_router(app_lifecycle.router, prefix="/api/v1")
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    if principal is not None:
        app.dependency_overrides[get_current_app] = lambda: principal
    return TestClient(app)


def _status(*, replayed: bool = False) -> dict:
    grant_id = str(uuid.uuid4())
    return {
        "installation_id": str(uuid.uuid4()),
        "app_id": str(uuid.uuid4()),
        "vault_id": str(uuid.uuid4()),
        "lifecycle": "installing",
        "latest_grant": {
            "id": grant_id,
            "generation": 1,
            "status": "active",
            "capabilities": ["installation:read"],
        },
        "latest_active_grant": {
            "id": grant_id,
            "generation": 1,
            "status": "active",
            "capabilities": ["installation:read"],
        },
        "resources": [{"kind": "table", "key": "owned-key", "status": "owned"}],
        "grant_generation": 1,
        "replayed": replayed,
        "command_status": "already_applied" if replayed else "accepted",
    }


def test_admin_put_returns_accepted_and_no_store(monkeypatch):
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    release_id = uuid.uuid4()
    auth_calls = []
    put_calls = []

    async def fake_authorize(user, **kwargs):
        auth_calls.append((user.username, kwargs))

    async def fake_put(requested_app_id, requested_vault_id, **kwargs):
        put_calls.append((requested_app_id, requested_vault_id, kwargs))
        return _status()

    monkeypatch.setattr(app_lifecycle, "authorize_lifecycle_admin", fake_authorize)
    monkeypatch.setattr(app_lifecycle, "put_installation", fake_put)
    response = _client(user=_user()).put(
        f"/api/v1/apps/{app_id}/installations/{vault_id}",
        json={
            "release_id": str(release_id),
            "capabilities": ["installation:read"],
        },
        headers={"x-correlation-id": "route-test"},
    )

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert auth_calls[0][1]["action"] == "app.installation.command"
    assert put_calls[0][0:2] == (app_id, vault_id)
    assert put_calls[0][2]["mode"] == "install"
    assert put_calls[0][2]["correlation_id"] == "route-test"


def test_replayed_put_returns_ok(monkeypatch):
    async def fake_authorize(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app_lifecycle, "authorize_lifecycle_admin", fake_authorize)

    async def fake_put(*_args, **_kwargs):
        return _status(replayed=True)

    monkeypatch.setattr(app_lifecycle, "put_installation", fake_put)
    response = _client(user=_user()).put(
        f"/api/v1/apps/{uuid.uuid4()}/installations/{uuid.uuid4()}",
        json={"release_id": str(uuid.uuid4()), "capabilities": ["inventory:read"]},
    )
    assert response.status_code == 200
    assert response.json()["command_status"] == "already_applied"


def test_denied_admin_request_stops_before_service(monkeypatch):
    called = False

    async def deny(*_args, **_kwargs):
        raise ForbiddenError("App request denied")

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(app_lifecycle, "authorize_lifecycle_admin", deny)
    monkeypatch.setattr(app_lifecycle, "put_installation", forbidden)
    response = _client(user=_user(is_admin=False)).put(
        f"/api/v1/apps/{uuid.uuid4()}/installations/{uuid.uuid4()}",
        json={"release_id": str(uuid.uuid4()), "capabilities": ["inventory:read"]},
    )
    assert response.status_code == 403
    assert response.json()["message"] == "App request denied"
    assert called is False


def test_app_status_uses_token_principal_and_no_store(monkeypatch):
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    principal = _principal(app_id)
    calls = []

    async def fake_status(requested_principal, *, vault_id, correlation_id):
        calls.append((requested_principal, vault_id, correlation_id))
        return _status()

    monkeypatch.setattr(
        app_lifecycle,
        "get_installation_status_for_app",
        fake_status,
    )
    response = _client(principal=principal).get(
        f"/api/v1/app/installations/{vault_id}?app_id={uuid.uuid4()}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls[0][0] is principal
    assert calls[0][1] == vault_id


def test_admin_status_preserves_grant_identity_and_resources(monkeypatch):
    expected = _status()

    async def fake_authorize(*_args, **_kwargs):
        return None

    async def fake_status(*_args, **_kwargs):
        return expected

    monkeypatch.setattr(app_lifecycle, "authorize_lifecycle_admin", fake_authorize)
    monkeypatch.setattr(app_lifecycle, "get_installation_status", fake_status)
    response = _client(user=_user()).get(
        f"/api/v1/apps/{expected['app_id']}/installations/{expected['vault_id']}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["latest_grant"]["id"] == expected["latest_grant"]["id"]
    assert body["latest_active_grant"]["id"] == expected["latest_active_grant"]["id"]
    assert body["resources"] == expected["resources"]
