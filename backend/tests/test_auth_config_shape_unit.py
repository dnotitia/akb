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
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)

    cfg = _call()
    assert cfg["local_auth"] == {"enabled": False}
    assert cfg["keycloak"]["enabled"] is True
    assert cfg["keycloak"]["sso_only"] is True
    assert cfg["keycloak"]["login_url"] == "/api/v1/auth/keycloak/login"
    assert cfg["keycloak"]["enrollment_mode"] == "invite_only"


def test_sso_only_forced_false_when_keycloak_disabled(monkeypatch):
    """An operator who mis-toggles `keycloak_sso_only: true` while
    Keycloak itself is off would otherwise strand every user at a
    broken redirect. The endpoint clamps sso_only to false here so
    the SPA stays on the local form."""
    monkeypatch.setattr(settings, "keycloak_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keycloak_sso_only", True, raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)

    cfg = _call()
    assert cfg["keycloak"]["enabled"] is False
    assert cfg["keycloak"]["sso_only"] is False
    assert cfg["keycloak"]["login_url"] is None
    assert cfg["local_auth"] == {"enabled": True}


def test_sso_only_default_false_in_hybrid_mode(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_sso_only", False, raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)

    cfg = _call()
    assert cfg["keycloak"]["enabled"] is True
    assert cfg["keycloak"]["sso_only"] is False
    assert cfg["local_auth"] == {"enabled": True}


def test_payload_publishes_exactly_the_three_capability_groups(monkeypatch):
    """The endpoint is UNAUTHENTICATED and documented as revealing no secrets,
    so the published key set is itself the contract. Pinning it exactly means a
    future field — a client secret, an internal URL, a tenant hint — cannot be
    added to this payload without a test saying so out loud."""
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_sso_only", False, raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)

    cfg = _call()
    assert set(cfg) == {"local_auth", "keycloak", "mcp_oauth"}
    assert set(cfg["local_auth"]) == {"enabled"}
    assert set(cfg["mcp_oauth"]) == {"enabled"}
    assert set(cfg["keycloak"]) == {"enabled", "enrollment_mode", "login_url", "sso_only"}


def test_mcp_oauth_group_tracks_its_setting_in_both_states(monkeypatch):
    """Connector UIs choose between the OAuth snippet and the PAT one from this
    group, so it is a published capability like the other two — pin both
    states rather than only the disabled one the other tests happen to set."""
    monkeypatch.setattr(settings, "keycloak_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keycloak_sso_only", False, raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", True, raising=False)

    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    assert _call()["mcp_oauth"] == {"enabled": True}

    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    assert _call()["mcp_oauth"] == {"enabled": False}


def test_enrollment_mode_is_published_but_inert_while_keycloak_is_off(monkeypatch):
    """`enrollment_mode` describes Keycloak enrollment, so it carries no policy
    while Keycloak is disabled — yet the endpoint still publishes the configured
    value rather than nulling it. That asymmetry is easy to misread: a consumer
    can see `invite_only` on a deployment where nothing enrolls through Keycloak
    at all. Pin the published-but-inert shape so the asymmetry is a stated
    contract (consumers must gate on `enabled` first) instead of an accident one
    refactor away from silently changing."""
    monkeypatch.setattr(settings, "keycloak_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keycloak_sso_only", False, raising=False)
    monkeypatch.setattr(settings, "local_auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)

    cfg = _call()
    assert cfg["keycloak"]["enabled"] is False
    assert cfg["keycloak"]["enrollment_mode"] == "invite_only"
    assert cfg["keycloak"]["login_url"] is None
    assert cfg["keycloak"]["sso_only"] is False
