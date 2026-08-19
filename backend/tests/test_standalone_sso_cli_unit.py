"""Secret-safe operator CLI contracts for bundled standalone SSO."""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio

_BOOTSTRAP_SECRET = "bootstrap-secret-never-print"  # pragma: allowlist secret
_UPGRADE_SECRET = "upgrade-secret-never-print"  # pragma: allowlist secret
_PRODUCT_PASSWORD = "product-password-never-print"  # pragma: allowlist secret
_MANAGEMENT_SECRET = "management-secret-never-print"  # pragma: allowlist secret
_API_SECRET = "api-secret-never-print"  # pragma: allowlist secret
_ADMIN_SECRET = "admin-secret-never-print"  # pragma: allowlist secret


def _patch_database(monkeypatch) -> None:
    from app import cli
    from app.db import postgres

    async def _initialize_operator_database() -> None:
        return None

    async def _close_pool() -> None:
        return None

    monkeypatch.setattr(
        cli,
        "_initialize_operator_database",
        _initialize_operator_database,
    )
    monkeypatch.setattr(postgres, "close_pool", _close_pool)


def _patch_sso_settings(monkeypatch) -> None:
    from app.config import settings

    values = {
        "auth_mode": "sso",
        "keycloak_enabled": True,
        "keycloak_server_url": "https://auth.akb.example.com",
        "keycloak_internal_url": "http://keycloak:8080",
        "keycloak_backchannel_logout_uri": (
            "http://backend:8000/api/v1/auth/keycloak/backchannel-logout"
        ),
        "keycloak_realm": "akb",
        "keycloak_client_id": "akb-web",
        "keycloak_client_secret": _API_SECRET,
        "keycloak_public_client": False,
        "keycloak_admin_client_id": "akb-admin",
        "keycloak_admin_client_secret": _ADMIN_SECRET,
        "keycloak_management_client_id": "akb-sso-manager",
        "keycloak_management_client_secret": _MANAGEMENT_SECRET,
        "keycloak_verify_ssl": True,
        "public_base_url": "https://akb.example.com",
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value, raising=False)


def _arguments(bootstrap_file: str, product_password_file: str) -> list[str]:
    return [
        "--bootstrap-client-id",
        "akb-bootstrap-temporary",
        "--bootstrap-client-secret-file",
        bootstrap_file,
        "--product-admin-username",
        "product-admin",
        "--product-admin-email",
        "product-admin@example.com",
        "--product-admin-password-file",
        product_password_file,
    ]


async def test_bootstrap_cli_reads_only_secret_files_and_emits_allowlisted_report(
    monkeypatch,
    tmp_path,
    capsys,
    caplog,
):
    from app import cli
    from app.services import standalone_sso_bootstrap, standalone_sso_keycloak

    _patch_database(monkeypatch)
    _patch_sso_settings(monkeypatch)
    bootstrap_file = tmp_path / "bootstrap-client-secret"
    upgrade_file = tmp_path / "upgrade-client-secret"
    product_password_file = tmp_path / "product-admin-password"
    bootstrap_file.write_text(f"{_BOOTSTRAP_SECRET}\n")
    upgrade_file.write_text(f"{_UPGRADE_SECRET}\n")
    product_password_file.write_text(f"{_PRODUCT_PASSWORD}\n")
    observed: dict[str, object] = {}

    class _Control:
        def __init__(self, *, verify_ssl: bool) -> None:
            observed["verify_ssl"] = verify_ssl

        async def aclose(self) -> None:
            observed["closed"] = True

    async def _bootstrap(
        spec,
        *,
        control,
        provision_admin,
        load_retirement_receipt,
        record_retirement_receipt,
    ):
        observed["spec"] = spec
        observed["control"] = control
        observed["provision_admin"] = provision_admin
        observed["load_retirement_receipt"] = load_retirement_receipt
        observed["record_retirement_receipt"] = record_retirement_receipt
        return {
            "mode": "fresh",
            "keycloak_mutated": True,
            "bootstrap_admin_retired": True,
            "realm_id": "akb-realm-id",
            "product_admin_subject": "product-admin-subject",
            "akb_user_id": "akb-user-id",
            "akb_admin_created": True,
            "active_signing_kid": "active-kid",
            "active_signing_bits": 3072,
            "passive_rs256_keys": 1,
            "management_roles": ["view-realm"],
        }

    monkeypatch.setattr(
        standalone_sso_keycloak,
        "KeycloakStandaloneSSOControl",
        _Control,
    )
    monkeypatch.setattr(
        standalone_sso_bootstrap,
        "bootstrap_standalone_sso",
        _bootstrap,
    )

    exit_code = await cli._bootstrap_standalone_sso(
        [
            *_arguments(str(bootstrap_file), str(product_password_file)),
            "--upgrade-client-id",
            "akb-bootstrap-upgrade-v2",
            "--upgrade-client-secret-file",
            str(upgrade_file),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    spec = observed["spec"]
    assert spec.bootstrap_client_secret == _BOOTSTRAP_SECRET
    assert spec.upgrade_client_id == "akb-bootstrap-upgrade-v2"
    assert spec.upgrade_client_secret == _UPGRADE_SECRET
    assert not hasattr(spec, "product_admin_password")
    assert spec.management_client_secret == _MANAGEMENT_SECRET
    assert spec.api_client_secret == _API_SECRET
    assert spec.admin_client_secret == _ADMIN_SECRET
    assert spec.backchannel_logout_uri == (
        "http://backend:8000/api/v1/auth/keycloak/backchannel-logout"
    )
    assert observed["verify_ssl"] is True
    assert observed["closed"] is True
    assert callable(observed["load_retirement_receipt"])
    assert callable(observed["record_retirement_receipt"])
    report = json.loads(captured.out)
    assert report["bootstrap_admin_retired"] is True
    assert report["active_signing_bits"] == 3072
    assert "product_admin_password" not in report
    rendered = captured.out + captured.err + caplog.text
    assert all(secret not in rendered for secret in _all_secrets())


async def test_bootstrap_cli_redacts_unexpected_exception_and_rejects_secret_args(
    monkeypatch,
    tmp_path,
    capsys,
):
    from app import cli
    from app.services import standalone_sso_bootstrap, standalone_sso_keycloak

    _patch_database(monkeypatch)
    _patch_sso_settings(monkeypatch)
    bootstrap_file = tmp_path / "bootstrap-client-secret"
    product_password_file = tmp_path / "product-admin-password"
    bootstrap_file.write_text(f"{_BOOTSTRAP_SECRET}\n")
    product_password_file.write_text(f"{_PRODUCT_PASSWORD}\n")

    class _Control:
        def __init__(self, *, verify_ssl: bool) -> None:
            assert verify_ssl is True

        async def aclose(self) -> None:
            return None

    async def _fail(*_args, **_kwargs):
        raise RuntimeError(" ".join(_all_secrets()))

    monkeypatch.setattr(
        standalone_sso_keycloak,
        "KeycloakStandaloneSSOControl",
        _Control,
    )
    monkeypatch.setattr(
        standalone_sso_bootstrap,
        "bootstrap_standalone_sso",
        _fail,
    )

    assert await cli._bootstrap_standalone_sso(
        _arguments(str(bootstrap_file), str(product_password_file))
    ) == 1
    captured = capsys.readouterr()
    assert "standalone_sso_bootstrap_failed" in captured.err
    assert all(secret not in captured.out + captured.err for secret in _all_secrets())

    assert await cli._bootstrap_standalone_sso(
        [
            *_arguments(str(bootstrap_file), str(product_password_file)),
            "--secret",
            _BOOTSTRAP_SECRET,
        ]
    ) == 2
    captured = capsys.readouterr()
    assert all(secret not in captured.out + captured.err for secret in _all_secrets())


async def test_bootstrap_cli_rerun_allows_removed_one_time_secret_files(
    monkeypatch,
    tmp_path,
    capsys,
):
    from app import cli
    from app.services import standalone_sso_bootstrap, standalone_sso_keycloak

    _patch_database(monkeypatch)
    _patch_sso_settings(monkeypatch)
    observed: dict[str, object] = {}

    class _Control:
        def __init__(self, *, verify_ssl: bool) -> None:
            assert verify_ssl is True

        async def aclose(self) -> None:
            return None

    async def _readback(spec, **_kwargs):
        observed["bootstrap_secret"] = spec.bootstrap_client_secret
        return {
            "mode": "readback",
            "keycloak_mutated": False,
            "bootstrap_admin_retired": True,
        }

    monkeypatch.setattr(
        standalone_sso_keycloak,
        "KeycloakStandaloneSSOControl",
        _Control,
    )
    monkeypatch.setattr(
        standalone_sso_bootstrap,
        "bootstrap_standalone_sso",
        _readback,
    )
    missing_bootstrap = tmp_path / "removed-bootstrap-secret"
    missing_password = tmp_path / "removed-product-password"

    assert await cli._bootstrap_standalone_sso(
        _arguments(str(missing_bootstrap), str(missing_password))
    ) == 0
    assert observed == {"bootstrap_secret": ""}
    assert json.loads(capsys.readouterr().out)["mode"] == "readback"


async def test_bootstrap_cli_rejects_reused_credentials_before_mutation(
    monkeypatch,
    tmp_path,
    capsys,
):
    from app import cli
    from app.config import settings

    _patch_database(monkeypatch)
    _patch_sso_settings(monkeypatch)
    monkeypatch.setattr(
        settings,
        "keycloak_admin_client_secret",
        _API_SECRET,
        raising=False,
    )
    bootstrap_file = tmp_path / "bootstrap-client-secret"
    product_password_file = tmp_path / "product-admin-password"
    bootstrap_file.write_text(f"{_BOOTSTRAP_SECRET}\n")
    product_password_file.write_text(f"{_PRODUCT_PASSWORD}\n")

    assert await cli._bootstrap_standalone_sso(
        _arguments(str(bootstrap_file), str(product_password_file))
    ) == 1

    captured = capsys.readouterr()
    assert "standalone_sso_input_invalid" in captured.err
    assert "independently generated" in captured.err
    assert all(secret not in captured.out + captured.err for secret in _all_secrets())


def _all_secrets() -> tuple[str, ...]:
    return (
        _BOOTSTRAP_SECRET,
        _UPGRADE_SECRET,
        _PRODUCT_PASSWORD,
        _MANAGEMENT_SECRET,
        _API_SECRET,
        _ADMIN_SECRET,
    )


async def test_bootstrap_cli_accepts_but_ignores_a_supplied_product_admin_password(
    monkeypatch,
    tmp_path,
    capsys,
    caplog,
):
    """The deployed init container still passes the retired flag.

    Accepting it and discarding the value is what lets the current manifests
    be redeployed unchanged: honouring it would put a stored credential back
    on the account this change is meant to leave un-enterable.
    """
    from app import cli
    from app.services import standalone_sso_bootstrap, standalone_sso_keycloak

    _patch_database(monkeypatch)
    _patch_sso_settings(monkeypatch)
    bootstrap_file = tmp_path / "bootstrap-client-secret"
    product_password_file = tmp_path / "product-admin-password"
    bootstrap_file.write_text(f"{_BOOTSTRAP_SECRET}\n")
    product_password_file.write_text(f"{_PRODUCT_PASSWORD}\n")
    observed: dict[str, object] = {}

    class _Control:
        def __init__(self, *, verify_ssl: bool) -> None:
            assert verify_ssl is True

        async def aclose(self) -> None:
            return None

    async def _bootstrap(spec, **_kwargs):
        observed["spec"] = spec
        return {"mode": "fresh", "bootstrap_admin_retired": True}

    monkeypatch.setattr(
        standalone_sso_keycloak,
        "KeycloakStandaloneSSOControl",
        _Control,
    )
    monkeypatch.setattr(
        standalone_sso_bootstrap,
        "bootstrap_standalone_sso",
        _bootstrap,
    )

    exit_code = await cli._bootstrap_standalone_sso(
        _arguments(str(bootstrap_file), str(product_password_file))
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    spec = observed["spec"]
    assert not hasattr(spec, "product_admin_password")
    assert _PRODUCT_PASSWORD not in repr(spec)
    rendered = captured.out + captured.err + caplog.text
    assert all(secret not in rendered for secret in _all_secrets())


async def test_bootstrap_cli_accepts_an_omitted_product_admin_password_flag(
    monkeypatch,
    tmp_path,
    capsys,
):
    from app import cli
    from app.services import standalone_sso_bootstrap, standalone_sso_keycloak

    _patch_database(monkeypatch)
    _patch_sso_settings(monkeypatch)
    bootstrap_file = tmp_path / "bootstrap-client-secret"
    bootstrap_file.write_text(f"{_BOOTSTRAP_SECRET}\n")

    class _Control:
        def __init__(self, *, verify_ssl: bool) -> None:
            assert verify_ssl is True

        async def aclose(self) -> None:
            return None

    async def _bootstrap(_spec, **_kwargs):
        return {"mode": "fresh", "bootstrap_admin_retired": True}

    monkeypatch.setattr(
        standalone_sso_keycloak,
        "KeycloakStandaloneSSOControl",
        _Control,
    )
    monkeypatch.setattr(
        standalone_sso_bootstrap,
        "bootstrap_standalone_sso",
        _bootstrap,
    )

    exit_code = await cli._bootstrap_standalone_sso(
        [
            "--bootstrap-client-id",
            "akb-bootstrap-temporary",
            "--bootstrap-client-secret-file",
            str(bootstrap_file),
            "--product-admin-username",
            "product-admin",
            "--product-admin-email",
            "product-admin@example.com",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "fresh"
