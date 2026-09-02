"""Focused authorization, wire-model, and resume route contracts."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.control_plane_models import AppDefinitionProjection, AppReleaseProjection
from app.api.deps import get_current_app, get_current_user
from app.api.routes import app_registry, app_rollouts
from app.services.app_identity_service import AppPrincipal
from app.services import app_resource_service as resources
from app.services import app_rollout_service as rollout
from app.services.app_registry_service import (
    _app_projection as project_registry_app,
    _canonical,
    _release_projection as project_registry_release,
)
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


def test_registry_release_request_rejects_v1_and_unknown_manifest_fields():
    client = _registry_client(_user(admin=True))
    base = {
        "version": "1.0.0",
        "manifest": {"steps": []},
        "manifest_checksum": "a" * 64,
    }

    old = client.post(
        f"/api/v1/apps/{uuid.uuid4()}/releases",
        json=base,
    )
    unknown = client.post(
        f"/api/v1/apps/{uuid.uuid4()}/releases",
        json={
            **base,
            "manifest": {"steps": [], "unexpected": True},
        },
    )
    assert old.status_code == 422
    assert unknown.status_code == 422


def test_registry_release_forwards_the_strict_v2_manifest(monkeypatch):
    app_id = uuid.uuid4()
    manifest = {
        "manifest_version": 2,
        "app_key": "generic-app",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": []},
        "transition_plans": [{"source": "fresh", "steps": []}],
    }
    calls: list[dict] = []

    async def fake_create(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "id": str(uuid.uuid4()),
            "app_id": str(app_id),
            "version": "1.0.0",
            "manifest": manifest,
            "manifest_checksum": "a" * 64,
            "registered_at": "2026-08-12T00:00:00Z",
            "replayed": False,
        }

    monkeypatch.setattr(app_registry, "create_app_release", fake_create)
    response = _registry_client(_user(admin=True)).post(
        f"/api/v1/apps/{app_id}/releases",
        json={
            "version": "1.0.0",
            "manifest": manifest,
            "manifest_checksum": "a" * 64,
        },
    )

    assert response.status_code == 200
    assert calls[0]["manifest"]["manifest_version"] == 2
    assert calls[0]["manifest"]["schema"]["tables"] == []


def test_registry_projections_decode_asyncpg_jsonb_strings():
    app_id = uuid.uuid4()
    release_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    metadata = {"owner": "validator", "flags": ["safe"]}
    manifest = {
        "manifest_version": 2,
        "app_key": "generic-app",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": [], "fingerprint": ""},
        "transition_plans": [{"source": "fresh", "steps": []}],
    }
    manifest["schema"]["fingerprint"] = resources.canonical_table_fingerprint([])
    release_checksum = rollout.manifest_checksum(manifest, version="1.0.0")

    app_projection = project_registry_app(
        {
            "id": app_id,
            "app_key": "generic-app",
            "display_name": "Generic App",
            "description": None,
            "metadata": '{"owner":"validator","flags":["safe"]}',
            "created_at": now,
            "updated_at": now,
        }
    )
    release_projection = project_registry_release(
        {
            "id": release_id,
            "app_id": app_id,
            "version": "1.0.0",
            "manifest": json.dumps(manifest),
            "manifest_checksum": release_checksum,
            "registered_at": now,
        }
    )

    assert _canonical('{"owner":"validator","flags":["safe"]}') == _canonical(metadata)
    assert app_projection["metadata"] == metadata
    assert release_projection["manifest"] == manifest
    AppDefinitionProjection.model_validate(app_projection)
    AppReleaseProjection.model_validate(release_projection)


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
