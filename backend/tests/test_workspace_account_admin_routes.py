"""Route-level contract for the managed account administration API."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.services.auth_service import AuthenticatedUser


pytestmark = pytest.mark.asyncio


def _user(*, admin: bool) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="platform-service",
        email="platform-service@service.akb.invalid",
        display_name=None,
        is_admin=admin,
        auth_method="pat",
        key_class="service",
    )


async def test_non_admin_is_rejected_before_account_service(monkeypatch):
    from app.api.routes import access

    async def _must_not_run(**_kwargs):
        raise AssertionError("service must not run for a non-admin")

    monkeypatch.setattr(access, "ensure_human_external_identity", _must_not_run)
    request = access.EnsureExternalIdentityRequest(
        issuer="https://id.example.com/realms/akb",
        subject="subject",
        email="member@example.com",
    )
    with pytest.raises(HTTPException) as exc_info:
        await access.admin_ensure_external_identity(request, _user(admin=False))
    assert exc_info.value.status_code == 403


async def test_ensure_external_identity_forwards_verified_actor(monkeypatch):
    from app.api.routes import access

    observed = {}

    async def _ensure(**kwargs):
        observed.update(kwargs)
        return {"user_id": "bound-user"}

    monkeypatch.setattr(access, "ensure_human_external_identity", _ensure)
    admin = _user(admin=True)
    request = access.EnsureExternalIdentityRequest(
        issuer="https://id.example.com/realms/akb",
        subject="subject",
        email="member@example.com",
        display_name="Member",
    )
    result = await access.admin_ensure_external_identity(request, admin)

    assert result == {"user_id": "bound-user"}
    assert observed == {
        "issuer": request.issuer,
        "subject": request.subject,
        "email": request.email,
        "display_name": request.display_name,
        "existing_user_id": None,
        "actor_id": admin.user_id,
    }


async def test_governance_routes_are_explicit_in_openapi():
    from app.main import app

    paths = app.openapi()["paths"]
    expected = {
        "/api/v1/admin/users/ensure-external-identity",
        "/api/v1/admin/users/by-external-identity",
        "/api/v1/admin/users/{user_id}/governance",
        "/api/v1/admin/service-users/ensure",
        "/api/v1/admin/users/{user_id}/role",
        "/api/v1/admin/users/{user_id}/suspend",
        "/api/v1/admin/users/{user_id}/activate",
        "/api/v1/admin/users/{user_id}/tokens/{token_id}",
    }
    assert expected <= set(paths)
    assert "/api/v1/admin/users/proxy" not in paths
