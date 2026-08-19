"""HTTP authorization contract for recovery-admin credential issue."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import access
from app.services.auth_service import AuthenticatedUser


_CREDENTIAL = "issued-credential-must-not-be-logged"  # pragma: allowlist secret


def _actor(**changes) -> AuthenticatedUser:
    values = {
        "user_id": str(uuid.uuid4()),
        "username": "break-glass-controller",
        "email": "break-glass-controller@service.invalid",
        "display_name": "Break-glass controller",
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
    async def _issue(**values):
        calls.append(values)
        return {
            "user_id": "00000000-0000-0000-0000-000000000072",
            "username": values["expected_username"],
            "email": values["expected_email"],
            "auth_mode": "local",
            "credential": _CREDENTIAL,
        }

    monkeypatch.setattr(
        access,
        "issue_recovery_admin_credential",
        _issue,
        raising=False,
    )
    app = FastAPI()
    app.include_router(access.router, prefix="/api/v1")
    app.dependency_overrides[access.get_current_user] = lambda: actor
    return TestClient(app)


def test_service_admin_token_issues_for_only_the_exact_expected_identity(monkeypatch):
    actor = _actor()
    calls: list[dict[str, str]] = []

    response = _client(monkeypatch, actor, calls).post(
        "/api/v1/admin/recovery-admin/issue-credential",
        json={
            "expected_username": "recovery-admin",
            "expected_email": "recovery-admin@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "00000000-0000-0000-0000-000000000072",
        "username": "recovery-admin",
        "email": "recovery-admin@example.com",
        "auth_mode": "local",
        "credential": _CREDENTIAL,
    }
    # The route names the API path; it cannot borrow the unauthenticated
    # break-glass discriminator.
    assert calls == [
        {
            "expected_username": "recovery-admin",
            "expected_email": "recovery-admin@example.com",
            "method": "recovery_admin_api",
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
def test_human_session_and_non_service_admin_carriers_fail_before_issue(monkeypatch, actor):
    calls: list[dict[str, str]] = []

    response = _client(monkeypatch, actor, calls).post(
        "/api/v1/admin/recovery-admin/issue-credential",
        json={
            "expected_username": "recovery-admin",
            "expected_email": "recovery-admin@example.com",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == (
        "recovery_admin_credential_requires_service_admin"
    )
    assert _CREDENTIAL not in response.text
    assert calls == []


def test_issue_request_rejects_an_unexpected_field(monkeypatch):
    calls: list[dict[str, str]] = []

    response = _client(monkeypatch, _actor(), calls).post(
        "/api/v1/admin/recovery-admin/issue-credential",
        json={
            "expected_username": "recovery-admin",
            "expected_email": "recovery-admin@example.com",
            "method": "recovery_admin_cli",
        },
    )

    assert response.status_code == 422
    assert calls == []
