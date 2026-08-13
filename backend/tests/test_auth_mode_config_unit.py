"""Canonical human-auth mode configuration boundary tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app import config as app_config


_VALID_SSO_EPOCH = "b71eb45d-1091-4673-a4bc-e3646d4dd279"


def _load(
    monkeypatch,
    tmp_path: Path,
    values: dict[str, object],
) -> app_config.Settings:
    (tmp_path / "app.yaml").write_text(yaml.safe_dump(values, sort_keys=False))
    monkeypatch.setattr(app_config, "_CONFIG_CANDIDATES", [tmp_path])
    return app_config._load_settings()


def _local_key_values(tmp_path: Path) -> dict[str, str]:
    from app.services.local_session_keys import generate_local_session_keyset

    key_dir = tmp_path / "local-session"
    generate_local_session_keyset(key_dir)
    return {
        "local_session_private_key_path": str(key_dir / "private.pem"),
        "local_session_jwks_path": str(key_dir / "jwks.json"),
    }


def test_runtime_load_accepts_explicit_local_auth_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    loaded = _load(monkeypatch, tmp_path, {"auth_mode": "local"})

    assert loaded.auth_mode == "local"
    assert loaded.local_human_auth_enabled is True
    assert loaded.sso_human_auth_enabled is False
    assert loaded.local_auth_enabled is True
    assert loaded.keycloak_sso_only is False


def test_runtime_load_accepts_explicit_sso_auth_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    loaded = _load(
        monkeypatch,
        tmp_path,
        {"auth_mode": "sso", "keycloak_enabled": True},
    )

    assert loaded.auth_mode == "sso"
    assert loaded.local_human_auth_enabled is False
    assert loaded.sso_human_auth_enabled is True
    assert loaded.local_auth_enabled is False
    assert loaded.keycloak_sso_only is True


def test_programmatic_settings_construction_does_not_require_runtime_mode() -> None:
    configured = app_config.Settings()

    assert configured.auth_mode is None
    assert configured.local_auth_enabled is True


def test_runtime_load_rejects_missing_mode_without_legacy_inference(
    monkeypatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(app_config.AuthModeConfigurationError) as exc_info:
        _load(
            monkeypatch,
            tmp_path,
            {"local_auth_enabled": True, "keycloak_enabled": False},
        )

    message = str(exc_info.value)
    assert "auth_mode is required" in message
    assert "local_only" in message
    assert "will not infer" in message


def test_runtime_load_rejects_unknown_mode_without_echoing_secrets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret = "must-not-appear-in-auth-mode-diagnostic"  # pragma: allowlist secret

    with pytest.raises(app_config.AuthModeConfigurationError) as exc_info:
        _load(
            monkeypatch,
            tmp_path,
            {"auth_mode": "hybrid", "jwt_secret": secret},
        )

    message = str(exc_info.value)
    assert "auth_mode must be 'local' or 'sso'" in message
    assert secret not in message


@pytest.mark.parametrize(
    ("values", "expected_detail"),
    [
        (
            {"auth_mode": "local", "local_auth_enabled": False},
            "local_auth_enabled=false",
        ),
        (
            {"auth_mode": "local", "keycloak_sso_only": True},
            "keycloak_sso_only=true",
        ),
        (
            {"auth_mode": "local", "keycloak_enabled": True},
            "requires mcp_oauth_enabled=true",
        ),
        (
            {
                "auth_mode": "sso",
                "keycloak_enabled": True,
                "local_auth_enabled": True,
            },
            "local_auth_enabled=true",
        ),
        (
            {
                "auth_mode": "sso",
                "keycloak_enabled": True,
                "keycloak_sso_only": False,
            },
            "keycloak_sso_only=false",
        ),
        (
            {"auth_mode": "sso", "keycloak_enabled": False},
            "requires keycloak_enabled=true",
        ),
    ],
)
def test_runtime_load_rejects_mode_contradictions(
    monkeypatch,
    tmp_path: Path,
    values: dict[str, object],
    expected_detail: str,
) -> None:
    secret = "must-not-appear-in-contradiction-diagnostic"  # pragma: allowlist secret
    values["db_password"] = secret

    with pytest.raises(app_config.AuthModeConfigurationError) as exc_info:
        _load(monkeypatch, tmp_path, values)

    message = str(exc_info.value)
    assert "contradicts" in message
    assert expected_detail in message
    assert secret not in message


def test_legacy_hybrid_is_diagnostic_only_and_runtime_rejects_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    classification = app_config.classify_legacy_auth_mode(
        local_auth_enabled=True,
        keycloak_enabled=True,
        keycloak_sso_only=False,
    )

    assert classification.status == "ambiguous_hybrid"
    assert classification.derived_mode is None

    with pytest.raises(app_config.AuthModeConfigurationError) as exc_info:
        _load(
            monkeypatch,
            tmp_path,
            {"local_auth_enabled": True, "keycloak_enabled": True},
        )

    assert "ambiguous_hybrid" in str(exc_info.value)


@pytest.mark.parametrize(
    ("local_enabled", "keycloak_enabled", "sso_only", "status", "derived"),
    [
        (True, False, False, "local_only", "local"),
        (False, True, False, "strict_sso", "sso"),
        (False, False, False, "invalid", None),
        (True, False, True, "invalid", None),
    ],
)
def test_legacy_classification_is_pure_and_never_activates_runtime(
    local_enabled: bool,
    keycloak_enabled: bool,
    sso_only: bool,
    status: str,
    derived: str | None,
) -> None:
    classification = app_config.classify_legacy_auth_mode(
        local_auth_enabled=local_enabled,
        keycloak_enabled=keycloak_enabled,
        keycloak_sso_only=sso_only,
    )

    assert classification.status == status
    assert classification.derived_mode == derived


def test_local_mode_keeps_keycloak_separate_for_mcp_oauth(
    monkeypatch,
    tmp_path: Path,
) -> None:
    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            **_local_key_values(tmp_path),
            "auth_mode": "local",
            "keycloak_enabled": True,
            "mcp_oauth_enabled": True,
        },
    )

    assert loaded.auth_mode == "local"
    assert loaded.local_human_auth_enabled is True
    assert loaded.sso_human_auth_enabled is False
    assert loaded.keycloak_enabled is True
    assert loaded.mcp_oauth_enabled is True


def test_canonical_runtime_rejects_deprecated_email_linking(
    monkeypatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(
        app_config.AuthModeConfigurationError,
        match="keycloak_link_by_email",
    ):
        _load(
            monkeypatch,
            tmp_path,
            {
                "auth_mode": "sso",
                "keycloak_enabled": True,
                "keycloak_link_by_email": True,
            },
        )


def test_local_mcp_oauth_startup_does_not_require_human_oidc_client_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            **_local_key_values(tmp_path),
            "auth_mode": "local",
            "keycloak_enabled": True,
            "mcp_oauth_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    lifecycle._validate_required_settings()


def test_sso_startup_requires_active_human_api_verifier_config_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "sso",
            "sso_session_epoch": _VALID_SSO_EPOCH,
            "keycloak_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "keycloak_client_id": "",
            "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    with pytest.raises(RuntimeError) as exc_info:
        lifecycle._validate_required_settings()

    message = str(exc_info.value)
    assert "human API client allowlist" in message
    assert "keycloak_redirect_uri" not in message
    assert "keycloak_client_secret" not in message


def test_sso_startup_defers_ordinary_browser_inputs_but_requires_admin_client(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "sso",
            "sso_session_epoch": _VALID_SSO_EPOCH,
            "keycloak_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "keycloak_client_id": "akb-web",
            "keycloak_redirect_uri": "",
            "keycloak_client_secret": "",
            "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    with pytest.raises(RuntimeError, match="keycloak_admin_client_secret"):
        lifecycle._validate_required_settings()


def test_sso_admin_client_is_dedicated_and_callback_is_deployment_bound(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "sso",
            "sso_session_epoch": _VALID_SSO_EPOCH,
            "keycloak_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "keycloak_client_id": "akb-web",
            "keycloak_admin_client_id": "akb-admin",
            "keycloak_admin_client_secret": "admin-client-secret",  # pragma: allowlist secret
            "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
            "keycloak_backchannel_logout_uri": ("http://backend:8000/api/v1/auth/keycloak/backchannel-logout"),
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    lifecycle._validate_required_settings()
    assert loaded.keycloak_admin_redirect_uri == ("https://akb.example.com/api/v1/admin/auth/keycloak/callback")
    assert loaded.keycloak_admin_post_logout_redirect_uri == ("https://akb.example.com/admin")
    assert loaded.keycloak_backchannel_logout_uri_effective == (
        "http://backend:8000/api/v1/auth/keycloak/backchannel-logout"
    )
    assert "akb-admin" not in loaded.keycloak_human_client_ids


def test_sso_backchannel_logout_uri_defaults_public_and_rejects_non_callback_targets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    common = {
        "auth_mode": "sso",
        "sso_session_epoch": _VALID_SSO_EPOCH,
        "keycloak_enabled": True,
        "keycloak_server_url": "https://auth.example.com",
        "keycloak_client_id": "akb-web",
        "keycloak_admin_client_id": "akb-admin",
        "keycloak_admin_client_secret": "admin-client-secret",  # pragma: allowlist secret
        "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
        "db_password": "test-db-password",  # pragma: allowlist secret
        "public_base_url": "https://akb.example.com",
    }
    defaulted = _load(monkeypatch, tmp_path, common)
    monkeypatch.setattr(lifecycle, "settings", defaulted)
    lifecycle._validate_required_settings()
    assert defaulted.keycloak_backchannel_logout_uri_effective == (
        "https://akb.example.com/api/v1/auth/keycloak/backchannel-logout"
    )

    for invalid in (
        "\nhttp://backend:8000/api/v1/auth/keycloak/backchannel-logout",
        "http://back end:8000/api/v1/auth/keycloak/backchannel-logout",
        "http://backend:/api/v1/auth/keycloak/backchannel-logout",
        "http://backend:8000/api/v1/auth/keycloak/backchannel-logout?",
        "http://backend:8000/api/v1/auth/keycloak/backchannel-logout#",
        "http://backend:8000/wrong",
        "http://backend:8000/api/v1/auth/keycloak/backchannel-logout?redirect=unsafe",
        "file:///api/v1/auth/keycloak/backchannel-logout",
        "http://user@backend:8000/api/v1/auth/keycloak/backchannel-logout",
        "http://0x7f000001/api/v1/auth/keycloak/backchannel-logout",
        "http://0x7f.0x0.0x0.0x1/api/v1/auth/keycloak/backchannel-logout",
        "http://0x7f.0.0.1/api/v1/auth/keycloak/backchannel-logout",
    ):
        configured = _load(
            monkeypatch,
            tmp_path,
            {**common, "keycloak_backchannel_logout_uri": invalid},
        )
        monkeypatch.setattr(lifecycle, "settings", configured)
        with pytest.raises(RuntimeError, match="back-channel logout URI"):
            lifecycle._validate_required_settings()

    dns_host = _load(
        monkeypatch,
        tmp_path,
        {
            **common,
            "keycloak_backchannel_logout_uri": ("http://0x7f.example.com:8000/api/v1/auth/keycloak/backchannel-logout"),
        },
    )
    monkeypatch.setattr(lifecycle, "settings", dns_host)
    lifecycle._validate_required_settings()


def test_sso_startup_rejects_admin_client_reuse_and_non_tls_public_origin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    reused = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "sso",
            "sso_session_epoch": _VALID_SSO_EPOCH,
            "keycloak_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "keycloak_client_id": "akb-web",
            "keycloak_admin_client_id": "akb-web",
            "keycloak_admin_client_secret": "admin-client-secret",  # pragma: allowlist secret
            "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", reused)
    with pytest.raises(RuntimeError, match="dedicated"):
        lifecycle._validate_required_settings()

    insecure = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "sso",
            "sso_session_epoch": _VALID_SSO_EPOCH,
            "keycloak_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "keycloak_client_id": "akb-web",
            "keycloak_admin_client_id": "akb-admin",
            "keycloak_admin_client_secret": "admin-client-secret",  # pragma: allowlist secret
            "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "http://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", insecure)
    with pytest.raises(RuntimeError, match="HTTPS"):
        lifecycle._validate_required_settings()

    insecure_issuer = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "sso",
            "sso_session_epoch": _VALID_SSO_EPOCH,
            "keycloak_enabled": True,
            "keycloak_server_url": "http://auth.example.com",
            "keycloak_client_id": "akb-web",
            "keycloak_admin_client_id": "akb-admin",
            "keycloak_admin_client_secret": "admin-client-secret",  # pragma: allowlist secret
            "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", insecure_issuer)
    with pytest.raises(RuntimeError, match="HTTPS public Keycloak"):
        lifecycle._validate_required_settings()


def test_sso_startup_rejects_missing_system_hmac_secret(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "sso",
            "sso_session_epoch": _VALID_SSO_EPOCH,
            "keycloak_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "keycloak_redirect_uri": "https://akb.example.com/auth/keycloak/callback",
            "keycloak_client_secret": "test-keycloak-client-secret",  # pragma: allowlist secret
            "keycloak_admin_client_secret": "test-admin-client-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    assert loaded.system_hmac_secret == ""
    assert loaded.app_token_secret == ""
    with pytest.raises(RuntimeError, match="system_hmac_secret"):
        lifecycle._validate_required_settings()


def test_sso_startup_accepts_legacy_jwt_secret_only_as_system_hmac_migration_input(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "sso",
            "sso_session_epoch": _VALID_SSO_EPOCH,
            "keycloak_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "keycloak_redirect_uri": "https://akb.example.com/auth/keycloak/callback",
            "keycloak_client_secret": "test-keycloak-client-secret",  # pragma: allowlist secret
            "keycloak_admin_client_secret": "test-admin-client-secret",  # pragma: allowlist secret
            "jwt_algorithm": "RS256",
            "jwt_secret": "compatibility-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    lifecycle._validate_required_settings()


def test_local_startup_requires_system_hmac_and_persistent_signing_keys(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "local",
            "app_token_secret": "independent-app-token-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    with pytest.raises(RuntimeError) as exc_info:
        lifecycle._validate_required_settings()
    message = str(exc_info.value)
    assert "system_hmac_secret" in message
    assert "local_session_private_key_path" in message
    assert "local_session_jwks_path" in message


def test_api_audience_has_distinct_public_resource_default() -> None:
    loaded = app_config.Settings(
        auth_mode="sso",
        keycloak_enabled=True,
        public_base_url="https://akb.example.com",
        mcp_oauth_enabled=True,
    )

    assert loaded.api_oauth_audience_effective == "https://akb.example.com/api"
    assert loaded.mcp_oauth_audience_effective == "https://akb.example.com/mcp"


def test_startup_rejects_equal_api_and_mcp_audiences(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    shared_audience = "https://akb.example.com/resource"
    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "local",
            "keycloak_enabled": True,
            "mcp_oauth_enabled": True,
            "keycloak_server_url": "https://auth.example.com",
            "api_oauth_audience": shared_audience,
            "mcp_oauth_audience": shared_audience,
            "jwt_secret": "test-jwt-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    with pytest.raises(RuntimeError, match="audiences must be distinct"):
        lifecycle._validate_required_settings()


def test_startup_rejects_hs256_after_local_v2_cutover(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.services import lifecycle

    loaded = _load(
        monkeypatch,
        tmp_path,
        {
            "auth_mode": "local",
            "jwt_algorithm": "HS256",
            "system_hmac_secret": "test-system-hmac-secret",  # pragma: allowlist secret
            "db_password": "test-db-password",  # pragma: allowlist secret
            "public_base_url": "https://akb.example.com",
        },
    )
    monkeypatch.setattr(lifecycle, "settings", loaded)

    with pytest.raises(RuntimeError, match="local-session-rs256-v2"):
        lifecycle._validate_required_settings()
