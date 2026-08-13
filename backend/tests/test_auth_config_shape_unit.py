"""Versioned public authentication-capability schema tests."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Response
import pytest

from app.api.routes import auth
from app.config import settings
from app.sso.keycloak_admin import ProviderControlError
from app.sso.models import ProviderReadback


def _provider(alias: str, state: str) -> ProviderReadback:
    return ProviderReadback(
        provider_type="keycloak-oidc",
        alias=alias,
        display_name=alias.title(),
        state=state,  # type: ignore[arg-type]
        enabled=state == "enabled",
        issuer=f"https://{alias}.example.com/realms/workforce",
        discovery_url=(
            f"https://{alias}.example.com/realms/workforce/"
            ".well-known/openid-configuration"
        ),
        client_id="akb-broker",
        client_secret_configured=True,
        redirect_uri=(
            f"https://auth.akb.example.com/realms/akb/broker/{alias}/endpoint"
        ),
        supports_logout=True,
        supports_identity_migration=False,
    )


class Control:
    control_mode = "direct"

    async def list_providers(self, **_kwargs):
        return (
            _provider("workforce", "enabled"),
            _provider("disabled", "configured_disabled"),
            _provider("broken", "configuration_error"),
        )


def _call() -> dict:
    return asyncio.run(auth.auth_config(Response()))


def test_v2_auth_config_is_never_http_cached(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    response = Response()

    asyncio.run(auth.auth_config(response))

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_v2_local_config_derives_human_capabilities_without_control_read(
    monkeypatch,
):
    class MustNotRead:
        @property
        def control_mode(self):
            raise AssertionError("local mode must not inspect Keycloak providers")

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(auth, "get_keycloak_provider_control", lambda: MustNotRead())

    assert _call() == {
        "schema_version": 2,
        "auth_mode": "local",
        "local_auth": {"enabled": True},
        "keycloak": {
            "enabled": False,
            "browser_session_ready": False,
        },
        "providers": [],
        "mcp_oauth": {"enabled": True},
    }


def test_v2_sso_config_lists_only_enabled_provider_buttons(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    monkeypatch.setattr(auth, "get_keycloak_provider_control", lambda: Control())

    assert _call() == {
        "schema_version": 2,
        "auth_mode": "sso",
        "local_auth": {"enabled": False},
        "keycloak": {
            "enabled": True,
            "browser_session_ready": False,
        },
        "providers": [
            {
                "provider_type": "keycloak-oidc",
                "alias": "workforce",
                "display_name": "Workforce",
                "login_url": None,
            }
        ],
        "mcp_oauth": {"enabled": False},
    }


def test_v2_public_config_has_only_non_secret_capability_fields(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(auth, "get_keycloak_provider_control", lambda: Control())

    cfg = _call()

    assert set(cfg) == {
        "schema_version",
        "auth_mode",
        "local_auth",
        "keycloak",
        "providers",
        "mcp_oauth",
    }
    assert set(cfg["local_auth"]) == {"enabled"}
    assert set(cfg["keycloak"]) == {"enabled", "browser_session_ready"}
    assert set(cfg["mcp_oauth"]) == {"enabled"}
    assert "secret" not in repr(cfg).lower()


@pytest.mark.parametrize("delegated", [True, False])
def test_v2_sso_config_fails_closed_without_a_verified_catalog(
    monkeypatch,
    delegated,
):
    class Unavailable:
        control_mode = "delegated" if delegated else "direct"

        async def list_providers(self, **_kwargs):
            raise ProviderControlError("keycloak_provider_list_failed")

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(auth, "get_keycloak_provider_control", lambda: Unavailable())

    with pytest.raises(HTTPException) as captured:
        _call()

    assert captured.value.status_code == 503
    assert captured.value.detail == "SSO provider catalog is unavailable"
