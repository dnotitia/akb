"""Versioned public authentication-capability schema tests."""

from __future__ import annotations

import asyncio

from app.config import settings


def _call() -> dict:
    from app.api.routes.auth import auth_config

    return asyncio.run(auth_config())


def test_v1_local_config_derives_human_capabilities_from_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)

    assert _call() == {
        "schema_version": 1,
        "auth_mode": "local",
        "local_auth": {"enabled": True},
        "keycloak": {
            "enabled": False,
            "browser_session_ready": False,
            "login_url": None,
        },
        "mcp_oauth": {"enabled": True},
    }


def test_v1_sso_config_is_explicitly_staged_without_login_url(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)

    assert _call() == {
        "schema_version": 1,
        "auth_mode": "sso",
        "local_auth": {"enabled": False},
        "keycloak": {
            "enabled": True,
            "browser_session_ready": False,
            "login_url": None,
        },
        "mcp_oauth": {"enabled": False},
    }


def test_v1_public_config_has_only_non_secret_capability_fields(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)

    cfg = _call()

    assert set(cfg) == {
        "schema_version",
        "auth_mode",
        "local_auth",
        "keycloak",
        "mcp_oauth",
    }
    assert set(cfg["local_auth"]) == {"enabled"}
    assert set(cfg["keycloak"]) == {
        "enabled",
        "browser_session_ready",
        "login_url",
    }
    assert set(cfg["mcp_oauth"]) == {"enabled"}
