"""Route ownership/idempotency/cache contracts for app rollouts."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_app, get_current_user
from app.api.routes import app_rollouts
from app.services.app_identity_service import AppPrincipal
from app.services.auth_service import AuthenticatedUser


def _user(admin: bool) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="operator",
        email="operator@example.invalid",
        display_name=None,
        is_admin=admin,
        auth_method="jwt",
    )


def _principal(app_id: uuid.UUID) -> AppPrincipal:
    return AppPrincipal(
        app_id=app_id,
        credential_id=uuid.uuid4(),
        credential_generation=1,
        deployment="test",
        token_id="token",
        expires_at=None,  # type: ignore[arg-type]
    )


def _client(*, user: AuthenticatedUser | None = None, principal: AppPrincipal | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(app_rollouts.router, prefix="/api/v1")
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    if principal is not None:
        app.dependency_overrides[get_current_app] = lambda: principal
    return TestClient(app)


def test_admin_rollout_requires_admin_before_scoped_lookup(monkeypatch):
    called = False

    async def lookup(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(app_rollouts, "get_rollout", lookup)
    response = _client(user=_user(False)).get(f"/api/v1/apps/{uuid.uuid4()}/rollouts/{uuid.uuid4()}")
    assert response.status_code == 403
    assert called is False


def test_admin_request_requires_uuid_idempotency_and_sets_no_store(monkeypatch):
    calls = []

    async def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"job_id": str(uuid.uuid4()), "replayed": False}

    monkeypatch.setattr(app_rollouts, "request_rollout_as_admin", fake_request)
    app_id = uuid.uuid4()
    release_id = uuid.uuid4()
    checksum = "a" * 64
    response = _client(user=_user(True)).post(
        f"/api/v1/apps/{app_id}/rollouts",
        json={"release_id": str(release_id), "manifest_checksum": checksum},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert calls and calls[0][1]["manifest_checksum_value"] == checksum


def test_app_request_is_bound_to_principal_app(monkeypatch):
    app_id = uuid.uuid4()
    calls = []

    async def fake_request(principal, **kwargs):
        calls.append((principal.app_id, kwargs))
        return {"job_id": str(uuid.uuid4()), "replayed": True}

    monkeypatch.setattr(app_rollouts, "request_rollout_as_app", fake_request)
    response = _client(principal=_principal(app_id)).post(
        "/api/v1/app/rollouts",
        json={"release_id": str(uuid.uuid4()), "manifest_checksum": "b" * 64},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls[0][0] == app_id
