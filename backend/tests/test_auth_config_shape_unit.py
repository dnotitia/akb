"""Pure-function tests for the `/api/v1/auth/config` payload shape.

The endpoint drives the SPA's render decisions (show local form vs
redirect to Keycloak; show OAuth toggle on connector UIs) so a wrong
shape ships a UX regression. These tests pin the contract directly
against the route handler.
"""
from __future__ import annotations

import asyncio

from app.config import settings


def _call() -> dict:
    from app.api.routes.auth import auth_config

    return asyncio.run(auth_config())


def test_sso_only_field_present_and_true_when_both_flags_on(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_sso_only", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)

    cfg = _call()
    assert cfg["keycloak"]["enabled"] is True
    assert cfg["keycloak"]["sso_only"] is True
    assert cfg["keycloak"]["login_url"] == "/api/v1/auth/keycloak/login"


def test_sso_only_forced_false_when_keycloak_disabled(monkeypatch):
    """An operator who mis-toggles `keycloak_sso_only: true` while
    Keycloak itself is off would otherwise strand every user at a
    broken redirect. The endpoint clamps sso_only to false here so
    the SPA stays on the local form."""
    monkeypatch.setattr(settings, "keycloak_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keycloak_sso_only", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)

    cfg = _call()
    assert cfg["keycloak"]["enabled"] is False
    assert cfg["keycloak"]["sso_only"] is False
    assert cfg["keycloak"]["login_url"] is None


def test_sso_only_default_false_in_hybrid_mode(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_sso_only", False, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)

    cfg = _call()
    assert cfg["keycloak"]["enabled"] is True
    assert cfg["keycloak"]["sso_only"] is False
