"""HTTP contracts for the legacy adoption control-plane surface."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.routes import app_legacy_adoptions
from app.services.auth_service import AuthenticatedUser


def _user(*, is_admin: bool = True) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="operator",
        email="operator@example.invalid",
        display_name=None,
        is_admin=is_admin,
        auth_method="jwt",
    )


def _projection(app_id: uuid.UUID, *, replayed: bool = False) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "adoption_id": str(uuid.uuid4()),
        "app_id": str(app_id),
        "baseline_release_id": str(uuid.uuid4()),
        "idempotency_key": str(uuid.uuid4()),
        "input_digest": "a" * 64,
        "status": "planned",
        "targets": [
            {
                "target_id": str(uuid.uuid4()),
                "vault_id": str(uuid.uuid4()),
                "table_allowlist": ["orders"],
                "expected_schema_fingerprint": "a" * 64,
                "state": "planned",
            }
        ],
        "created_at": now,
        "updated_at": now,
        "replayed": replayed,
    }


def _client(*, user: AuthenticatedUser | None = None) -> TestClient:
    application = FastAPI()
    application.include_router(app_legacy_adoptions.router, prefix="/api/v1")
    if user is not None:
        application.dependency_overrides[get_current_user] = lambda: user
    return TestClient(application)


def test_create_replay_changes_201_to_200_and_never_caches(monkeypatch) -> None:
    app_id = uuid.uuid4()
    calls: list[dict] = []

    async def fake_create(*_args, **kwargs):
        calls.append(kwargs)
        return _projection(app_id, replayed=len(calls) > 1)

    monkeypatch.setattr(app_legacy_adoptions, "create_legacy_adoption", fake_create)
    client = _client(user=_user())
    body = {
        "baseline_release_id": str(uuid.uuid4()),
        "targets": [
            {
                "vault_id": str(uuid.uuid4()),
                "table_allowlist": ["orders"],
            }
        ],
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}

    first = client.post(f"/api/v1/apps/{app_id}/legacy-adoptions", json=body, headers=headers)
    replay = client.post(f"/api/v1/apps/{app_id}/legacy-adoptions", json=body, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert replay.headers["pragma"] == "no-cache"
    assert calls[0]["baseline_release_id"] == uuid.UUID(body["baseline_release_id"])
    assert calls[0]["idempotency_key"] == headers["Idempotency-Key"]


def test_status_and_apply_forward_ids_and_set_no_store(monkeypatch) -> None:
    app_id = uuid.uuid4()
    adoption_id = uuid.uuid4()
    calls: list[tuple[str, uuid.UUID, uuid.UUID]] = []

    async def fake_get(app, adoption, **_kwargs):
        calls.append(("get", app, adoption))
        return _projection(app)

    async def fake_apply(app, adoption, **_kwargs):
        calls.append(("apply", app, adoption))
        result = _projection(app)
        result["status"] = "applied"
        result["outcome"] = "applied"
        return result

    monkeypatch.setattr(app_legacy_adoptions, "get_legacy_adoption", fake_get)
    monkeypatch.setattr(app_legacy_adoptions, "apply_legacy_adoption", fake_apply)
    client = _client(user=_user())

    status_response = client.get(f"/api/v1/apps/{app_id}/legacy-adoptions/{adoption_id}")
    apply_response = client.post(f"/api/v1/apps/{app_id}/legacy-adoptions/{adoption_id}/apply")

    assert status_response.status_code == 200
    assert apply_response.status_code == 200
    assert status_response.headers["cache-control"] == "no-store"
    assert apply_response.headers["pragma"] == "no-cache"
    assert calls == [("get", app_id, adoption_id), ("apply", app_id, adoption_id)]


def test_request_validation_rejects_non_uuid_idempotency_and_caller_fingerprint() -> None:
    app_id = uuid.uuid4()
    client = _client(user=_user())
    response = client.post(
        f"/api/v1/apps/{app_id}/legacy-adoptions",
        json={
            "baseline_release_id": str(uuid.uuid4()),
            "targets": [
                {
                    "vault_id": str(uuid.uuid4()),
                    "table_allowlist": ["orders"],
                    "expected_schema_fingerprint": "not-a-fingerprint",
                }
            ],
        },
        headers={"Idempotency-Key": "not-a-uuid"},
    )
    assert response.status_code == 422
