"""Secret-handling contracts for the recovery-admin operator CLI."""

from __future__ import annotations

import json
import stat

import pytest


pytestmark = pytest.mark.asyncio

_SECRET = "generated-secret-must-never-be-printed"  # pragma: allowlist secret


def _result(*, mode: str, created: bool = True) -> dict:
    return {
        "user_id": "11111111-1111-4111-8111-111111111111",
        "username": "recovery-admin",
        "email": "recovery-admin@example.com",
        "auth_mode": mode,
        "created": created,
        "is_admin": True,
        "is_recovery_admin": True,
    }


def _patch_database(monkeypatch):
    from app import cli
    from app.db import postgres

    async def _initialize_operator_database():
        return None

    async def _close_pool():
        return None

    monkeypatch.setattr(
        cli,
        "_initialize_operator_database",
        _initialize_operator_database,
    )
    monkeypatch.setattr(postgres, "close_pool", _close_pool)


async def test_operator_database_initialization_installs_role_sync(monkeypatch):
    from app import cli
    from app.db import postgres
    from app.services import role_sync

    events: list[object] = []
    pool = object()

    async def _init_db():
        events.append("init-db")

    async def _get_pool():
        events.append("get-pool")
        return pool

    class _RoleSync:
        def __init__(self, actual_pool):
            assert actual_pool is pool
            events.append("construct-role-sync")

    def _set_role_sync(instance):
        assert isinstance(instance, _RoleSync)
        events.append("set-role-sync")

    monkeypatch.setattr(postgres, "init_db", _init_db)
    monkeypatch.setattr(postgres, "get_pool", _get_pool)
    monkeypatch.setattr(role_sync, "RoleSync", _RoleSync)
    monkeypatch.setattr(role_sync, "set_role_sync", _set_role_sync)

    await cli._initialize_operator_database()

    assert events == [
        "init-db",
        "get-pool",
        "construct-role-sync",
        "set-role-sync",
    ]


async def test_generated_password_is_only_written_to_requested_0600_file(
    monkeypatch,
    tmp_path,
    capsys,
    caplog,
):
    from app import cli
    from app.services import recovery_admin_service

    _patch_database(monkeypatch)
    monkeypatch.setattr(cli.secrets, "token_urlsafe", lambda _size: _SECRET)
    observed = {}

    async def _provision(**kwargs):
        observed.update(kwargs)
        return _result(mode="local")

    monkeypatch.setattr(
        recovery_admin_service,
        "provision_local_recovery_admin",
        _provision,
    )
    password_path = tmp_path / "recovery-password"

    exit_code = await cli._provision_recovery_admin(
        [
            "local",
            "--username",
            "recovery-admin",
            "--email",
            "recovery-admin@example.com",
            "--generate-password-file",
            str(password_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert observed["password"] == _SECRET
    assert password_path.read_text() == f"{_SECRET}\n"
    assert stat.S_IMODE(password_path.stat().st_mode) == 0o600
    assert _SECRET not in captured.out
    assert _SECRET not in captured.err
    assert _SECRET not in caplog.text
    report = json.loads(captured.out)
    assert report["password_file_written"] is True
    assert "password" not in report


async def test_supplied_password_file_is_not_echoed(monkeypatch, tmp_path, capsys):
    from app import cli
    from app.services import recovery_admin_service

    _patch_database(monkeypatch)
    source = tmp_path / "mounted-secret"
    source.write_text(f"{_SECRET}\n")
    observed = {}

    async def _provision(**kwargs):
        observed.update(kwargs)
        return _result(mode="local")

    monkeypatch.setattr(
        recovery_admin_service,
        "provision_local_recovery_admin",
        _provision,
    )

    exit_code = await cli._provision_recovery_admin(
        [
            "local",
            "--username",
            "recovery-admin",
            "--email",
            "recovery-admin@example.com",
            "--password-file",
            str(source),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert observed["password"] == _SECRET
    assert _SECRET not in captured.out
    assert _SECRET not in captured.err
    assert json.loads(captured.out)["password_file_written"] is False


async def test_generated_file_is_removed_on_conflict_without_secret_disclosure(
    monkeypatch,
    tmp_path,
    capsys,
):
    from app import cli
    from app.exceptions import RecoveryAdminConflictError
    from app.services import recovery_admin_service

    _patch_database(monkeypatch)
    monkeypatch.setattr(cli.secrets, "token_urlsafe", lambda _size: _SECRET)

    async def _conflict(**_kwargs):
        raise RecoveryAdminConflictError()

    monkeypatch.setattr(
        recovery_admin_service,
        "provision_local_recovery_admin",
        _conflict,
    )
    password_path = tmp_path / "recovery-password"

    exit_code = await cli._provision_recovery_admin(
        [
            "local",
            "--username",
            "recovery-admin",
            "--email",
            "recovery-admin@example.com",
            "--generate-password-file",
            str(password_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert not password_path.exists()
    assert "recovery_admin_conflict" in captured.err
    assert _SECRET not in captured.out
    assert _SECRET not in captured.err


async def test_generated_file_is_removed_when_exact_identity_already_exists(
    monkeypatch,
    tmp_path,
    capsys,
):
    from app import cli
    from app.services import recovery_admin_service

    _patch_database(monkeypatch)
    monkeypatch.setattr(cli.secrets, "token_urlsafe", lambda _size: _SECRET)

    async def _already_provisioned(**_kwargs):
        return _result(mode="local", created=False)

    monkeypatch.setattr(
        recovery_admin_service,
        "provision_local_recovery_admin",
        _already_provisioned,
    )
    password_path = tmp_path / "unused-recovery-password"

    exit_code = await cli._provision_recovery_admin(
        [
            "local",
            "--username",
            "recovery-admin",
            "--email",
            "recovery-admin@example.com",
            "--generate-password-file",
            str(password_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert not password_path.exists()
    assert json.loads(captured.out)["password_file_written"] is False
    assert _SECRET not in captured.out
    assert _SECRET not in captured.err


async def test_sso_profile_accepts_only_external_identity_and_usage_errors_hide_values(
    monkeypatch,
    capsys,
):
    from app import cli
    from app.services import recovery_admin_service

    _patch_database(monkeypatch)
    observed = {}

    async def _provision(**kwargs):
        observed.update(kwargs)
        return _result(mode="sso")

    monkeypatch.setattr(
        recovery_admin_service,
        "provision_sso_recovery_admin",
        _provision,
    )
    valid_args = [
        "sso",
        "--username",
        "recovery-admin",
        "--email",
        "recovery-admin@example.com",
        "--issuer",
        "https://issuer.example.com/realms/akb",
        "--subject",
        "stable-admin-subject",
    ]

    assert await cli._provision_recovery_admin(valid_args) == 0
    assert observed == {
        "username": "recovery-admin",
        "email": "recovery-admin@example.com",
        "issuer": "https://issuer.example.com/realms/akb",
        "subject": "stable-admin-subject",
    }
    capsys.readouterr()

    assert await cli._provision_recovery_admin(
        [*valid_args, "--password", _SECRET]
    ) == 2
    captured = capsys.readouterr()
    assert _SECRET not in captured.out
    assert _SECRET not in captured.err


async def test_break_glass_issue_calls_the_shared_service_and_reveals_once(monkeypatch, capsys):
    from app import cli
    from app.services import recovery_admin_service

    _patch_database(monkeypatch)
    observed = {}

    async def _issue(**kwargs):
        observed.update(kwargs)
        return {
            "user_id": "11111111-1111-4111-8111-111111111111",
            "username": "recovery-admin",
            "email": "recovery-admin@example.com",
            "auth_mode": "local",
            "credential": _SECRET,
        }

    monkeypatch.setattr(
        recovery_admin_service,
        "issue_recovery_admin_credential",
        _issue,
    )

    exit_code = await cli._issue_recovery_admin_credential(
        [
            "--expected-username",
            "recovery-admin",
            "--expected-email",
            "recovery-admin@example.com",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    # Same service function as the endpoint, distinguished only by method,
    # and with no authenticated principal.
    assert observed == {
        "expected_username": "recovery-admin",
        "expected_email": "recovery-admin@example.com",
        "method": "recovery_admin_cli",
    }
    # The one-time reveal is the whole point of the command, so it is on
    # stdout — but it must not be in the machine-readable report, which is
    # what a caller piping stdout into a log or a file will keep.
    assert _SECRET in captured.out
    lines = captured.out.splitlines()
    report = json.loads(lines[0])
    assert report == {
        "user_id": "11111111-1111-4111-8111-111111111111",
        "username": "recovery-admin",
        "email": "recovery-admin@example.com",
        "auth_mode": "local",
    }
    assert _SECRET not in lines[0]
    assert any(_SECRET in line for line in lines[1:])


async def test_break_glass_issue_hides_values_on_usage_and_service_errors(monkeypatch, capsys):
    from app import cli
    from app.exceptions import RecoveryAdminCredentialConflictError
    from app.services import recovery_admin_service

    _patch_database(monkeypatch)

    async def _refuse(**_kwargs):
        raise RecoveryAdminCredentialConflictError()

    monkeypatch.setattr(
        recovery_admin_service,
        "issue_recovery_admin_credential",
        _refuse,
    )

    assert await cli._issue_recovery_admin_credential(
        [
            "--expected-username",
            "recovery-admin",
            "--expected-email",
            "recovery-admin@example.com",
        ]
    ) == 1
    captured = capsys.readouterr()
    assert "recovery_admin_credential_conflict" in captured.err
    assert _SECRET not in captured.out + captured.err

    assert await cli._issue_recovery_admin_credential(["--expected-username", "only"]) == 2
    captured = capsys.readouterr()
    assert _SECRET not in captured.out + captured.err


async def test_break_glass_issue_is_registered_as_a_subcommand(capsys):
    import asyncio

    from app import cli

    # `main` runs its own event loop, so drive it off this test's loop.
    # It also returns 2 for an unknown subcommand, so the exit code alone
    # cannot tell "registered but misused" from "not registered at all".
    assert await asyncio.to_thread(cli.main, ["issue-recovery-admin-credential"]) == 2
    captured = capsys.readouterr()
    assert "Unknown subcommand" not in captured.err
    assert "--expected-username" in captured.err

    assert await asyncio.to_thread(cli.main, ["issue-recovery-admin-credentials"]) == 2
    assert "Unknown subcommand" in capsys.readouterr().err
