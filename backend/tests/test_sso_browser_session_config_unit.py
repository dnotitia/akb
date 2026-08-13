"""Configuration boundary for the ordinary SSO browser-session capability."""

from __future__ import annotations

import asyncio
import base64
import uuid

import pytest

from app.config import Settings
from app.services import auth_policy, lifecycle, sso_browser_session_service
from app.services.auth_service import AuthenticatedUser


def _encoded_key(byte: int = 7) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")


def _sso_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "auth_mode": "sso",
        "keycloak_enabled": True,
        "keycloak_server_url": "https://auth.example.com",
        "keycloak_client_id": "akb-web",
        "keycloak_client_secret": "browser-client-secret",  # pragma: allowlist secret
        "keycloak_admin_client_id": "akb-admin",
        "keycloak_admin_client_secret": "admin-client-secret",  # pragma: allowlist secret
        "system_hmac_secret": "system-hmac-secret",  # pragma: allowlist secret
        "db_password": "database-secret",  # pragma: allowlist secret
        "public_base_url": "https://akb.example.com",
    }
    values.update(changes)
    return Settings(**values)


def test_browser_session_capability_requires_complete_server_custody(monkeypatch):
    missing_key = _sso_settings()
    monkeypatch.setattr(auth_policy, "settings", missing_key)
    assert auth_policy.sso_browser_session_ready() is False

    ready = _sso_settings(sso_browser_session_encryption_key=_encoded_key())
    monkeypatch.setattr(auth_policy, "settings", ready)
    assert auth_policy.sso_browser_session_ready() is True
    assert ready.keycloak_browser_redirect_uri == ("https://akb.example.com/api/v1/auth/keycloak/callback")

    local = Settings(
        auth_mode="local",
        sso_browser_session_encryption_key=_encoded_key(),
    )
    monkeypatch.setattr(auth_policy, "settings", local)
    assert auth_policy.sso_browser_session_ready() is False


def test_public_pkce_client_can_activate_without_a_client_secret(monkeypatch):
    configured = _sso_settings(
        keycloak_public_client=True,
        keycloak_client_secret="",
        sso_browser_session_encryption_key=_encoded_key(),
    )
    monkeypatch.setattr(auth_policy, "settings", configured)

    assert auth_policy.sso_browser_session_ready() is True


def test_invalid_configured_key_fails_startup_without_echoing_it(monkeypatch):
    invalid_key = "must-not-appear-in-diagnostic"  # pragma: allowlist secret
    configured = _sso_settings(sso_browser_session_encryption_key=invalid_key)
    monkeypatch.setattr(lifecycle, "settings", configured)

    with pytest.raises(RuntimeError, match="sso_browser_session_encryption_key") as captured:
        lifecycle._validate_required_settings()

    assert invalid_key not in str(captured.value)


def test_missing_key_keeps_expand_contract_deployment_staged(monkeypatch):
    configured = _sso_settings()
    monkeypatch.setattr(lifecycle, "settings", configured)

    lifecycle._validate_required_settings()


def test_session_lifetime_must_fit_inside_absolute_lifetime():
    with pytest.raises(ValueError, match="idle_ttl_secs"):
        _sso_settings(
            sso_browser_session_idle_ttl_secs=7200,
            sso_browser_session_absolute_ttl_secs=3600,
        )


def test_https_uses_host_locked_cookie_names_and_http_uses_isolated_dev_names(
    monkeypatch,
):
    monkeypatch.setattr(
        sso_browser_session_service.settings,
        "public_base_url",
        "https://akb.example.com",
        raising=False,
    )
    assert sso_browser_session_service.sso_browser_session_cookie_name().startswith("__Host-")
    assert sso_browser_session_service.sso_browser_csrf_cookie_name().startswith("__Host-")

    monkeypatch.setattr(
        sso_browser_session_service.settings,
        "public_base_url",
        "http://127.0.0.1:3000",
        raising=False,
    )
    assert sso_browser_session_service.sso_browser_session_cookie_name() == ("akb_dev_sso_session")
    assert sso_browser_session_service.sso_browser_csrf_cookie_name() == ("akb_dev_sso_csrf")


@pytest.mark.asyncio
async def test_refresh_waiters_are_bounded_before_acquiring_the_locked_pass(
    monkeypatch,
):
    active = 0
    maximum = 0

    async def resolve_pass(_token_hash, *, allow_refresh, **_kwargs):
        nonlocal active, maximum
        if not allow_refresh:
            return None, True
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return (
            AuthenticatedUser(
                user_id=str(uuid.uuid4()),
                username="alice",
                email="alice@example.com",
                display_name="Alice",
                is_admin=False,
                auth_method="browser_session",
            ),
            False,
        )

    monkeypatch.setattr(
        sso_browser_session_service,
        "_resolve_sso_browser_session_pass",
        resolve_pass,
    )
    monkeypatch.setattr(
        sso_browser_session_service,
        "_sso_browser_session_needs_refresh",
        lambda _token_hash: asyncio.sleep(0, result=True),
    )
    await asyncio.gather(
        *(
            sso_browser_session_service.resolve_sso_browser_session(f"session-token-{index:02d}-that-is-long-enough")
            for index in range(10)
        )
    )

    assert maximum == sso_browser_session_service._BROWSER_REFRESH_CONCURRENCY
