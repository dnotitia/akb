"""HTTP ownership, status-code, app-scope, and cache contracts."""

from __future__ import annotations

import uuid
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.api.deps import get_current_app, get_current_user
from app.api.routes import app_installations

settings.git_storage_path = tempfile.mkdtemp(prefix="akb-installation-routes-test-")

from app.main import app as main_app
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
        token_id="app-token-id",
        expires_at=None,  # type: ignore[arg-type]
    )


def _client(*, user: AuthenticatedUser | None = None, principal: AppPrincipal | None = None):
    app = FastAPI()
    app.include_router(app_installations.router, prefix="/api/v1")
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    if principal is not None:
        app.dependency_overrides[get_current_app] = lambda: principal
    return TestClient(app)


def _projection(app_id: uuid.UUID, vault_id: uuid.UUID) -> dict:
    return {
        "installation_id": str(uuid.uuid4()),
        "app_id": str(app_id),
        "vault_id": str(vault_id),
        "lifecycle": "installing",
        "desired_grant_generation": 1,
        "command_status": "not_applicable",
    }


def test_install_returns_202_and_replay_returns_200(monkeypatch):
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    calls: list[dict] = []

    async def fake_command(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        replayed = len(calls) == 2
        return {
            **_projection(app_id, vault_id),
            "command_status": "already_applied" if replayed else "accepted",
            "replayed": replayed,
        }

    monkeypatch.setattr(app_installations, "command_installation", fake_command)
    client = _client(user=_user())
    body = {
        "release_id": str(uuid.uuid4()),
        "capabilities": ["installation:read"],
    }
    first = client.put(f"/api/v1/apps/{app_id}/installations/{vault_id}", json=body)
    second = client.put(f"/api/v1/apps/{app_id}/installations/{vault_id}", json=body)

    assert first.status_code == 202
    assert second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert second.headers["pragma"] == "no-cache"
    assert calls[0]["kwargs"]["mode"] == "install"


def test_app_status_uses_principal_app_and_cannot_select_another_app(monkeypatch):
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    requested: list[tuple] = []

    async def fake_status(principal, requested_vault_id, **kwargs):
        requested.append((principal.app_id, requested_vault_id, kwargs))
        return _projection(principal.app_id, requested_vault_id)

    monkeypatch.setattr(app_installations, "get_app_installation_status", fake_status)
    client = _client(principal=_principal(app_id))
    response = client.get(
        f"/api/v1/app/installations/{vault_id}?app_id={uuid.uuid4()}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert requested[0][0] == app_id
    assert requested[0][1] == vault_id


def test_delete_replay_status_and_no_store(monkeypatch):
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()

    async def fake_uninstall(*_args, **_kwargs):
        return {
            **_projection(app_id, vault_id),
            "lifecycle": "uninstalled",
            "command_status": "already_applied",
            "replayed": True,
        }

    monkeypatch.setattr(app_installations, "uninstall_installation", fake_uninstall)
    response = _client(user=_user()).delete(
        f"/api/v1/apps/{app_id}/installations/{vault_id}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["lifecycle"] == "uninstalled"


def test_lifecycle_auth_errors_are_also_no_store():
    response = TestClient(main_app).get(
        f"/api/v1/app/installations/{uuid.uuid4()}"
    )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
