"""Server-side local-auth policy regression tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.exceptions import LocalAuthDisabledError


pytestmark = pytest.mark.asyncio


async def _must_not_touch_db():
    raise AssertionError("local-auth denial must happen before database access")


def _must_not_hash_or_verify(*_args, **_kwargs):
    raise AssertionError("local-auth denial must happen before password work")


async def test_register_denies_before_hash_or_database(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(auth_service, "get_pool", _must_not_touch_db)
    monkeypatch.setattr(auth_service, "hash_password", _must_not_hash_or_verify)

    with pytest.raises(LocalAuthDisabledError) as exc_info:
        await auth_service.register("member", "member@example.com", "known-password")

    assert exc_info.value.code == "local_auth_disabled"


async def test_login_denies_before_database_or_password_verification(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(auth_service, "get_pool", _must_not_touch_db)
    monkeypatch.setattr(auth_service, "verify_password", _must_not_hash_or_verify)

    with pytest.raises(LocalAuthDisabledError):
        await auth_service.login("member", "known-password")


async def test_change_password_denies_before_input_validation_or_database(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(auth_service, "get_pool", _must_not_touch_db)

    with pytest.raises(LocalAuthDisabledError):
        await auth_service.change_password(
            "00000000-0000-0000-0000-000000000001",
            "known-password",
            "short",
        )


async def test_reset_password_denies_admin_and_cli_before_database(monkeypatch):
    from app.services import password_service

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(password_service, "get_pool", _must_not_touch_db)
    monkeypatch.setattr(password_service, "generate_temp_password", _must_not_hash_or_verify)

    for method in ("admin_ui", "cli"):
        with pytest.raises(LocalAuthDisabledError):
            await password_service.reset_password(
                username="member",
                actor_id=None,
                method=method,
            )


async def test_cli_reset_password_reports_policy_denial_without_traceback(
    monkeypatch,
    capsys,
):
    from app import cli

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)

    result = await cli._reset_password("member")

    assert result == 1
    assert "local_auth_disabled" in capsys.readouterr().err


async def test_default_policy_keeps_local_auth_enabled():
    assert settings.require_auth_mode() == "local"


async def test_revoke_all_sessions_service_denies_before_database(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(auth_service, "get_pool", _must_not_touch_db)

    with pytest.raises(LocalAuthDisabledError):
        await auth_service.revoke_all_sessions(
            "00000000-0000-0000-0000-000000000001"
        )


async def test_local_lifecycle_routes_deny_before_service_calls(monkeypatch):
    from app.api.routes import access, auth
    from app.services.auth_service import AuthenticatedUser

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    forbidden = AsyncMock(side_effect=AssertionError("route must reject before service"))
    monkeypatch.setattr(auth, "register", forbidden)
    monkeypatch.setattr(auth, "login", forbidden)
    monkeypatch.setattr(auth, "revoke_all_sessions", forbidden)
    monkeypatch.setattr(access, "revoke_all_sessions", forbidden)
    actor = AuthenticatedUser(
        user_id="00000000-0000-0000-0000-000000000001",
        username="admin",
        email="admin@example.com",
        display_name="Admin",
        is_admin=True,
        auth_method="oauth",
    )

    calls = [
        lambda: auth.register_user(
            auth.RegisterRequest(
                username="member",
                email="member@example.com",
                password="known-password",
            )
        ),
        lambda: auth.login_user(
            auth.LoginRequest(username="member", password="known-password")
        ),
        lambda: auth.change_password_route(
            auth.ChangePasswordRequest(
                current_password="known-password",
                new_password="new-known-password",
            ),
            actor,
        ),
        lambda: auth.revoke_my_sessions(actor),
        lambda: access.admin_revoke_user_sessions(actor.user_id, actor),
        lambda: access.admin_reset_user_password(actor.user_id, actor),
    ]
    for call in calls:
        with pytest.raises(LocalAuthDisabledError):
            await call()

    forbidden.assert_not_awaited()


async def test_pat_management_remains_available_in_sso_mode(monkeypatch):
    from app.api.routes import auth
    from app.services.auth_service import AuthenticatedUser

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    actor = AuthenticatedUser(
        user_id="00000000-0000-0000-0000-000000000001",
        username="member",
        email="member@example.com",
        display_name="Member",
        is_admin=False,
        auth_method="oauth",
    )
    create = AsyncMock(return_value={"token": "akb_real_pat"})
    monkeypatch.setattr(auth, "create_pat", create)

    result = await auth.create_token(auth.CreatePATRequest(name="automation"), actor)

    assert result == {"token": "akb_real_pat"}
    create.assert_awaited_once()
