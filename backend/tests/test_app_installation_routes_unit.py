"""HTTP ownership, status-code, app-scope, and cache contracts."""

from __future__ import annotations

import logging
import uuid
import tempfile

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.api import deps
from app.api.deps import get_current_app, get_current_user
from app.api.routes import app_installations
from app.services import app_installation_service as installation

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


def test_initial_grant_approval_returns_202_and_replay_returns_200(monkeypatch):
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    release_id = uuid.uuid4()
    calls: list[dict] = []

    async def fake_approval(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        replayed = len(calls) == 2
        return {
            **_projection(app_id, vault_id),
            "command_status": "already_applied" if replayed else "accepted",
            "replayed": replayed,
        }

    monkeypatch.setattr(app_installations, "approve_initial_installation_grant", fake_approval)
    client = _client(user=_user())
    body = {
        "baseline_release_id": str(release_id),
        "capabilities": ["installation:read"],
    }
    first = client.post(
        f"/api/v1/apps/{app_id}/installations/{vault_id}/grant",
        json=body,
    )
    second = client.post(
        f"/api/v1/apps/{app_id}/installations/{vault_id}/grant",
        json=body,
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert second.headers["pragma"] == "no-cache"
    assert calls[0]["kwargs"]["baseline_release_id"] == release_id


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


def test_member_active_status_uses_user_session_and_only_returns_active(monkeypatch):
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    user = _user(is_admin=False)
    requested: list[dict] = []

    async def fake_status(requested_app_id, requested_vault_id, **kwargs):
        requested.append(
            {
                "app_id": requested_app_id,
                "vault_id": requested_vault_id,
                **kwargs,
            }
        )
        return {"active": True}

    monkeypatch.setattr(
        app_installations, "get_member_installation_active_status", fake_status
    )
    response = _client(user=user).get(
        f"/api/v1/apps/{app_id}/installations/{vault_id}/active"
    )

    assert response.status_code == 200
    assert response.json() == {"active": True}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert requested[0]["app_id"] == app_id
    assert requested[0]["vault_id"] == vault_id
    assert requested[0]["user"] is user
    assert isinstance(requested[0]["correlation_id"], str)


def test_member_active_status_query_failure_is_safe_and_no_store(monkeypatch, caplog):
    user = _user(is_admin=False)
    vault_id = uuid.uuid4()
    raw_error_marker = "RAW_MEMBER_INSTALLATION_QUERY_ERROR"
    raw_credential = "synthetic-fault-credential-abcdef"
    raw_error = f"{raw_error_marker}; Authorization: Bearer {raw_credential}"

    class FakeConnection:
        async def fetchrow(self, _query, requested_vault_id):
            assert requested_vault_id == vault_id
            return {
                "id": vault_id,
                "name": "member-status-vault",
                "owner_id": uuid.UUID(user.user_id),
            }

        async def fetchval(self, query, *_args):
            assert "FROM vault_app_installations" in query
            raise asyncpg.exceptions.UndefinedTableError(raw_error)

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    async def fake_get_pool():
        return FakePool()

    async def allow_owner_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(installation, "get_pool", fake_get_pool)
    monkeypatch.setattr(installation, "check_vault_access", allow_owner_access)
    monkeypatch.setitem(main_app.dependency_overrides, get_current_user, lambda: user)

    with caplog.at_level(logging.ERROR):
        response = TestClient(main_app, raise_server_exceptions=False).get(
            f"/api/v1/apps/{uuid.uuid4()}/installations/{vault_id}/active"
        )

    payload = response.json()
    observed = {
        "status": response.status_code,
        "message": payload.get("message"),
        "error": payload.get("error"),
        "code": payload.get("code"),
        "detail_message": payload.get("detail", {}).get("message")
        if isinstance(payload.get("detail"), dict)
        else payload.get("detail"),
        "cache_control": response.headers.get("cache-control"),
        "pragma": response.headers.get("pragma"),
        "raw_error_in_response": raw_error_marker in response.text,
        "raw_credential_in_response": raw_credential in response.text,
        "raw_error_in_logs": raw_error_marker in caplog.text,
        "raw_credential_in_logs": raw_credential in caplog.text,
    }
    assert observed == {
        "status": 503,
        "message": "Installation status is temporarily unavailable",
        "error": "Installation status is temporarily unavailable",
        "code": "member_installation_status_unavailable",
        "detail_message": "Installation status is temporarily unavailable",
        "cache_control": "no-store",
        "pragma": "no-cache",
        "raw_error_in_response": False,
        "raw_credential_in_response": False,
        "raw_error_in_logs": False,
        "raw_credential_in_logs": False,
    }


def test_member_active_status_invalid_session_is_no_store():
    response = TestClient(main_app).get(
        f"/api/v1/apps/{uuid.uuid4()}/installations/{uuid.uuid4()}/active"
    )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_member_active_status_requires_pat_read_scope(monkeypatch):
    user = _user(is_admin=False)
    user.token_scopes = frozenset({"write"})

    async def authorize(_authorization):
        return user

    monkeypatch.setattr(deps, "resolve_rest_user_authorization", authorize)
    response = TestClient(main_app).get(
        f"/api/v1/apps/{uuid.uuid4()}/installations/{uuid.uuid4()}/active",
        headers={"Authorization": "Bearer fixture-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"
    assert response.json()["detail"]["required_scope"] == "read"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


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
