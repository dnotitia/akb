"""Unit tests for the Keycloak SSO post-login redirect logic.

Pure-function tests (no DB, no Keycloak) covering the cross-origin
companion-app allowlist and the open-redirect guard. The single security
invariant under test: the post-login one-time code can leave akb's own
origin ONLY for an origin explicitly in
``settings.keycloak_post_login_allowed_origins``; everything else collapses
to the safe same-site path.
"""
from __future__ import annotations

import urllib.parse

import pytest

from app.api.routes import auth
from app.config import settings


@pytest.fixture
def allow(monkeypatch):
    """Set the companion-origin allowlist for one test."""
    def _set(origins: list[str]) -> None:
        monkeypatch.setattr(
            settings, "keycloak_post_login_allowed_origins", origins, raising=False
        )
    return _set


# ── _normalize_origin ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://reef.example.com", "https://reef.example.com"),
        ("https://reef.example.com/cb?x=1", "https://reef.example.com"),
        ("https://Reef.Example.COM/cb", "https://reef.example.com"),  # host lowercased
        ("http://localhost:5173/cb", "http://localhost:5173"),        # port kept
        # Not absolute http(s) URLs → None.
        ("/auth/callback", None),
        ("//evil.com", None),
        ("", None),
        (None, None),
        ("ftp://reef.example.com", None),
        ("javascript:alert(1)", None),
        # Userinfo spoof: real host is evil.com, must NOT normalize to trusted.
        ("https://reef.example.com@evil.com/cb", None),
    ],
)
def test_normalize_origin(value, expected):
    assert auth._normalize_origin(value) == expected


# ── _allowed_companion_origin ────────────────────────────────────────

def test_empty_allowlist_blocks_everything(allow):
    allow([])
    assert auth._allowed_companion_origin("https://reef.example.com/cb") is None
    assert auth._allowed_companion_origin("/auth/callback") is None


def test_listed_origin_allowed(allow):
    allow(["https://reef.example.com"])
    assert (
        auth._allowed_companion_origin("https://reef.example.com/api/auth/cb?next=/x")
        == "https://reef.example.com"
    )


def test_unlisted_origin_blocked(allow):
    allow(["https://reef.example.com"])
    assert auth._allowed_companion_origin("https://evil.com/cb") is None


def test_userinfo_spoof_blocked_even_when_prefix_listed(allow):
    # Listing the trusted origin must not let an attacker smuggle it as
    # userinfo in front of their own host.
    allow(["https://reef.example.com"])
    assert auth._allowed_companion_origin("https://reef.example.com@evil.com/cb") is None


def test_allowlist_entries_normalized(allow):
    # A sloppily-configured entry (trailing path, mixed case) still matches
    # because both sides go through _normalize_origin.
    allow(["https://Reef.Example.com/ignored"])
    assert (
        auth._allowed_companion_origin("https://reef.example.com/cb")
        == "https://reef.example.com"
    )


# ── _post_login_target ───────────────────────────────────────────────

def test_target_same_site_default(allow):
    allow([])
    target = auth._post_login_target("/dashboard", "CODE123")
    assert target.startswith(settings.keycloak_post_login_path + "?")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
    assert q["code"] == ["CODE123"]
    assert q["redirect"] == ["/dashboard"]


def test_target_open_redirect_collapses(allow):
    # Absolute URL but NOT allowlisted → must not leave the same-site path.
    allow([])
    target = auth._post_login_target("https://evil.com/steal", "CODE123")
    assert target.startswith(settings.keycloak_post_login_path + "?")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
    assert q["redirect"] == ["/"]  # collapsed by _safe_redirect_path


def test_target_companion_origin_gets_code(allow):
    allow(["https://reef.example.com"])
    target = auth._post_login_target(
        "https://reef.example.com/api/auth/akb/sso/callback", "CODE123"
    )
    parts = urllib.parse.urlsplit(target)
    assert f"{parts.scheme}://{parts.netloc}" == "https://reef.example.com"
    assert parts.path == "/api/auth/akb/sso/callback"
    q = urllib.parse.parse_qs(parts.query)
    assert q["code"] == ["CODE123"]


def test_target_companion_preserves_existing_query(allow):
    allow(["https://reef.example.com"])
    target = auth._post_login_target(
        "https://reef.example.com/cb?next=%2Fdash", "CODE123"
    )
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
    assert q["next"] == ["/dash"]
    assert q["code"] == ["CODE123"]


def test_target_unlisted_absolute_collapses_to_root(allow):
    allow(["https://reef.example.com"])
    target = auth._post_login_target("https://other.example.com/cb", "CODE123")
    assert target.startswith(settings.keycloak_post_login_path + "?")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
    assert q["redirect"] == ["/"]
