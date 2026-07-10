"""Server-side local-auth policy regression tests."""

from __future__ import annotations

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

    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)
    monkeypatch.setattr(auth_service, "get_pool", _must_not_touch_db)
    monkeypatch.setattr(auth_service, "hash_password", _must_not_hash_or_verify)

    with pytest.raises(LocalAuthDisabledError) as exc_info:
        await auth_service.register("member", "member@example.com", "known-password")

    assert exc_info.value.code == "local_auth_disabled"


async def test_login_denies_before_database_or_password_verification(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)
    monkeypatch.setattr(auth_service, "get_pool", _must_not_touch_db)
    monkeypatch.setattr(auth_service, "verify_password", _must_not_hash_or_verify)

    with pytest.raises(LocalAuthDisabledError):
        await auth_service.login("member", "known-password")


async def test_change_password_denies_before_input_validation_or_database(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)
    monkeypatch.setattr(auth_service, "get_pool", _must_not_touch_db)

    with pytest.raises(LocalAuthDisabledError):
        await auth_service.change_password(
            "00000000-0000-0000-0000-000000000001",
            "known-password",
            "short",
        )


async def test_reset_password_denies_admin_and_cli_before_database(monkeypatch):
    from app.services import password_service

    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)
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

    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)

    result = await cli._reset_password("member")

    assert result == 1
    assert "local_auth_disabled" in capsys.readouterr().err


async def test_default_policy_keeps_local_auth_enabled():
    assert type(settings).model_fields["local_auth_enabled"].default is True
