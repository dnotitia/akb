"""Focused authorization, wire-model, and resume route contracts."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_app, get_current_user
from app.api.routes import app_registry, app_rollouts
from app.services.app_identity_service import AppPrincipal
from app.services.auth_service import AuthenticatedUser


def _user(*, admin: bool) -> AuthenticatedUser:
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
        deployment="unit",
        token_id="token-id",
        expires_at=None,  # type: ignore[arg-type]
    )


def _registry_client(user: AuthenticatedUser) -> TestClient:
    application = FastAPI()
    application.include_router(app_registry.router, prefix="/api/v1")
    application.dependency_overrides[get_current_user] = lambda: user
    return TestClient(application)


def _resume_client(
    *, user: AuthenticatedUser | None = None, principal: AppPrincipal | None = None
) -> TestClient:
    application = FastAPI()
    application.include_router(app_rollouts.router, prefix="/api/v1")
    if user is not None:
        application.dependency_overrides[get_current_user] = lambda: user
    if principal is not None:
        application.dependency_overrides[get_current_app] = lambda: principal
    return TestClient(application)


def _app_projection(app_id: uuid.UUID) -> dict[str, object]:
    now = "2026-08-12T00:00:00Z"
    return {
        "id": str(app_id),
        "app_key": "generic-app",
        "display_name": "Generic App",
        "description": None,
        "metadata": {},
        "created_at": now,
        "updated_at": now,
        "replayed": False,
    }


def test_registry_write_requires_system_admin_before_service_lookup(monkeypatch):
    called = False

    async def unexpected(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(app_registry, "create_app_definition", unexpected)
    response = _registry_client(_user(admin=False)).post(
        "/api/v1/apps", json={"app_key": "generic-app"}
    )
    assert response.status_code == 403
    assert called is False


def test_registry_create_uses_explicit_projection_and_no_store(monkeypatch):
    app_id = uuid.uuid4()
    calls: list[dict[str, object]] = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return _app_projection(app_id)

    monkeypatch.setattr(app_registry, "create_app_definition", fake_create)
    response = _registry_client(_user(admin=True)).post(
        "/api/v1/apps",
        json={"app_key": "generic-app", "metadata": {"owner": "unit"}},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert calls[0]["app_key"] == "generic-app"
    assert response.json()["id"] == str(app_id)


def test_admin_resume_creates_new_attempt_ack(monkeypatch):
    app_id = uuid.uuid4()
    source_id = uuid.uuid4()
    release_id = uuid.uuid4()
    key = str(uuid.uuid4())
    calls: list[dict[str, object]] = []

    async def fake_resume(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {"job_id": str(uuid.uuid4()), "replayed": False}

    monkeypatch.setattr(app_rollouts, "resume_rollout_as_admin", fake_resume)
    response = _resume_client(user=_user(admin=True)).post(
        f"/api/v1/apps/{app_id}/rollouts/{source_id}/resume",
        json={"release_id": str(release_id), "manifest_checksum": "a" * 64},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert calls[0]["args"][:2] == (app_id, source_id)
    assert calls[0]["kwargs"]["idempotency_key"] == key


def test_app_resume_is_bound_to_principal_and_replay_is_200(monkeypatch):
    app_id = uuid.uuid4()
    source_id = uuid.uuid4()
    principal = _principal(app_id)
    calls: list[dict[str, object]] = []

    async def fake_resume(requested_principal, *args, **kwargs):
        calls.append({"principal": requested_principal, "args": args, "kwargs": kwargs})
        return {"job_id": str(uuid.uuid4()), "replayed": True}

    monkeypatch.setattr(app_rollouts, "resume_rollout_as_app", fake_resume)
    response = _resume_client(principal=principal).post(
        f"/api/v1/app/rollouts/{source_id}/resume",
        json={"release_id": str(uuid.uuid4()), "manifest_checksum": "b" * 64},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 200
    assert calls[0]["principal"] is principal
    assert calls[0]["args"] == (source_id,)
