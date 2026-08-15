"""HTTP authorization contract for recovery-admin retirement."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import access
from app.services.auth_service import AuthenticatedUser


def _actor(**changes) -> AuthenticatedUser:
    values = {
        "user_id": str(uuid.uuid4()),
        "username": "retirement-controller",
        "email": "retirement-controller@service.invalid",
        "display_name": "Retirement controller",
        "is_admin": True,
        "auth_method": "pat",
        "account_kind": "service",
        "token_id": str(uuid.uuid4()),
        "key_class": "service",
        "token_scopes": frozenset({"write"}),
    }
    values.update(changes)
    return AuthenticatedUser(**values)


def _client(monkeypatch, actor: AuthenticatedUser, calls: list[dict[str, str]]) -> TestClient:
    async def _retire(**values):
        calls.append(values)
        return {
            "user_id": "00000000-0000-0000-0000-000000000071",
            "username": values["expected_username"],
            "email": values["expected_email"],
            "account_status": "suspended",
            "is_admin": False,
            "is_recovery_admin": False,
            "account_kind": "human",
            "auth_provider": "local",
        }

    monkeypatch.setattr(access, "retire_local_recovery_admin", _retire, raising=False)
    app = FastAPI()
    app.include_router(access.router, prefix="/api/v1")
    app.dependency_overrides[access.get_current_user] = lambda: actor
    return TestClient(app)


def test_service_admin_token_can_retire_only_the_exact_expected_identity(monkeypatch):
    actor = _actor()
    calls: list[dict[str, str]] = []

    response = _client(monkeypatch, actor, calls).post(
        "/api/v1/admin/recovery-admin/retire",
        json={
            "expected_username": "local-recovery-admin",
            "expected_email": "local-recovery-admin@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "00000000-0000-0000-0000-000000000071",
        "username": "local-recovery-admin",
        "email": "local-recovery-admin@example.com",
        "account_status": "suspended",
        "is_admin": False,
        "is_recovery_admin": False,
        "account_kind": "human",
        "auth_provider": "local",
    }
    assert calls == [
        {
            "expected_username": "local-recovery-admin",
            "expected_email": "local-recovery-admin@example.com",
            "actor_user_id": actor.user_id,
            "actor_token_id": actor.token_id,
        }
    ]


@pytest.mark.parametrize(
    "actor",
    [
        _actor(auth_method="jwt", account_kind="human", token_id=None, key_class=None),
        _actor(account_kind="human", key_class="pat"),
        _actor(auth_method="browser_session", token_id=None, key_class=None),
        _actor(is_admin=False),
        _actor(token_id=None),
        _actor(key_class="publishable"),
    ],
)
def test_human_session_and_non_service_admin_carriers_fail_before_retirement(
    monkeypatch,
    actor,
):
    calls: list[dict[str, str]] = []

    response = _client(monkeypatch, actor, calls).post(
        "/api/v1/admin/recovery-admin/retire",
        json={
            "expected_username": "local-recovery-admin",
            "expected_email": "local-recovery-admin@example.com",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == (
        "recovery_admin_retirement_requires_service_admin"
    )
    assert calls == []
