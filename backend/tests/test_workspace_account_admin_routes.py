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


def _bootstrap_user(*, auth_method: str = "pat", token_id: str | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="platform-bot",
        email="platform-bot@workspace.local",
        display_name="Platform Bot",
        is_admin=True,
        auth_method=auth_method,
        token_id=token_id,
        key_class="pat",
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


async def test_prepare_external_identity_uses_distinct_fail_closed_route(monkeypatch):
    from app.api.routes import access

    observed = {}

    async def _ensure(**kwargs):
        observed.update(kwargs)
        return {"user_id": "prepared-user", "account_status": "suspended"}

    monkeypatch.setattr(access, "ensure_human_external_identity", _ensure)
    admin = _user(admin=True)
    request = access.EnsureExternalIdentityRequest(
        issuer="https://id.example.com/realms/akb",
        subject="prepared-subject",
        email="prepared@example.com",
        display_name="Prepared member",
    )

    result = await access.admin_prepare_external_identity(request, admin)

    assert result["account_status"] == "suspended"
    assert observed == {
        "issuer": request.issuer,
        "subject": request.subject,
        "email": request.email,
        "display_name": request.display_name,
        "existing_user_id": None,
        "prepare_suspended": True,
        "actor_id": admin.user_id,
    }


async def test_token_identification_is_admin_only_and_never_returns_raw_token(monkeypatch):
    from app.api.routes import access

    raw_token = "akb_legacy-secret-material"
    observed = {}

    async def _identify(token, *, actor_id):
        observed.update(token=token, actor_id=actor_id)
        return {"user_id": "owner-id", "token_id": "token-id"}

    monkeypatch.setattr(access, "identify_user_token", _identify)
    request = access.IdentifyTokenRequest(token=raw_token)

    with pytest.raises(HTTPException) as exc_info:
        await access.admin_identify_token(request, _user(admin=False))
    assert exc_info.value.status_code == 403
    assert observed == {}

    admin = _user(admin=True)
    result = await access.admin_identify_token(request, admin)
    assert result == {"user_id": "owner-id", "token_id": "token-id"}
    assert raw_token not in repr(request)
    assert raw_token not in repr(result)
    assert observed == {"token": raw_token, "actor_id": admin.user_id}


async def test_admin_mint_forwards_caller_selected_token_id(monkeypatch):
    from app.api.routes import access
    from app.db import postgres
    from app.services import auth_service

    user_id = uuid.uuid4()
    token_id = uuid.uuid4()
    observed = {}

    class _Connection:
        async def fetchrow(self, _query, resolved_user_id):
            assert resolved_user_id == user_id
            return {"id": user_id}

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def _get_pool():
        return _Pool()

    async def _create_pat(resolved_user_id, name, **kwargs):
        observed.update(user_id=resolved_user_id, name=name, **kwargs)
        return {"token_id": str(token_id), "token": "akb_once"}

    monkeypatch.setattr(postgres, "get_pool", _get_pool)
    monkeypatch.setattr(auth_service, "create_pat", _create_pat)
    request = access.AdminManagedMintTokenRequest(
        name="claude-code",
        token_id=str(token_id),
    )

    result = await access.admin_mint_managed_user_token(
        str(user_id), request, _user(admin=True)
    )

    assert result["token_id"] == str(token_id)
    assert observed["token_id"] == str(token_id)
    assert observed["user_id"] == str(user_id)


async def test_adopt_current_service_requires_exact_admin_pat_and_forwards_ids(monkeypatch):
    from app.api.routes import access

    observed = {}

    async def _adopt(**kwargs):
        observed.update(kwargs)
        return {"account_kind": "service", "key_class": "service"}

    monkeypatch.setattr(access, "adopt_current_admin_as_service", _adopt)
    request = access.AdoptCurrentServiceUserRequest(
        expected_username="platform-bot",
        expected_email="platform-bot@workspace.local",
    )

    with pytest.raises(HTTPException) as exc_info:
        await access.admin_adopt_current_service_user(
            request,
            _bootstrap_user(auth_method="jwt"),
        )
    assert exc_info.value.status_code == 409
    assert observed == {}

    token_id = str(uuid.uuid4())
    actor = _bootstrap_user(token_id=token_id)
    result = await access.admin_adopt_current_service_user(request, actor)
    assert result == {"account_kind": "service", "key_class": "service"}
    assert observed == {
        "user_id": actor.user_id,
        "token_id": token_id,
        "expected_username": request.expected_username,
        "expected_email": request.expected_email,
        "actor_id": actor.user_id,
    }


async def test_auth_me_exposes_additive_current_key_class():
    from app.api.routes import auth

    actor = _bootstrap_user(token_id=str(uuid.uuid4()))
    actor.key_class = "service"
    result = await auth.me(actor)
    assert result["user_id"] == actor.user_id
    assert result["key_class"] == "service"


async def test_governance_routes_are_explicit_in_openapi():
    from app.main import app

    paths = app.openapi()["paths"]
    expected = {
        "/api/v1/admin/users/ensure-external-identity",
        "/api/v1/admin/users/prepare-external-identity",
        "/api/v1/admin/users/by-external-identity",
        "/api/v1/admin/users/{user_id}/governance",
        "/api/v1/admin/service-users/ensure",
        "/api/v1/admin/service-users/adopt-current",
        "/api/v1/admin/users/{user_id}/role",
        "/api/v1/admin/users/{user_id}/suspend",
        "/api/v1/admin/users/{user_id}/activate",
        "/api/v1/admin/users/{user_id}/tokens/{token_id}",
        "/api/v1/admin/users/{user_ref}/managed-tokens",
        "/api/v1/admin/tokens/identify",
    }
    assert expected <= set(paths)
    assert "/api/v1/admin/users/proxy" not in paths
