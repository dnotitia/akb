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
from starlette.requests import Request

from app.api.routes import auth
from app.config import settings


def test_sso_error_reason_exposes_only_stable_account_codes():
    from app.api.routes.auth import _public_sso_error_reason
    from app.exceptions import (
        AccountSuspendedError,
        AKBError,
        ExternalIdentityConflictError,
        MembershipRequiredError,
    )

    assert _public_sso_error_reason(MembershipRequiredError()) == "membership_required"
    assert _public_sso_error_reason(AccountSuspendedError()) == "account_suspended"
    assert _public_sso_error_reason(ExternalIdentityConflictError()) == "identity_conflict"
    assert _public_sso_error_reason(AKBError("provider detail")) == "auth_failed"


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


# ── failed companion callback target ────────────────────────────────


def test_error_target_companion_gets_stable_code_and_preserves_query(allow):
    allow(["https://reef.example.com"])
    target = auth._post_login_error_target(
        "https://reef.example.com/api/auth/akb/sso/callback?redirect=%2Fdone",
        "membership_required",
    )
    parts = urllib.parse.urlsplit(target)
    assert f"{parts.scheme}://{parts.netloc}" == "https://reef.example.com"
    assert parts.path == "/api/auth/akb/sso/callback"
    assert urllib.parse.parse_qs(parts.query) == {
        "redirect": ["/done"],
        "sso_error": ["membership_required"],
    }


def test_error_target_unlisted_absolute_never_leaves_akb(allow):
    allow(["https://reef.example.com"])
    target = auth._post_login_error_target(
        "https://evil.example.com/steal", "membership_required"
    )
    assert target == "/auth?sso_error=membership_required"


@pytest.mark.asyncio
async def test_callback_returns_account_error_to_allowed_companion(allow, monkeypatch):
    from app.exceptions import MembershipRequiredError
    from app.services import keycloak_oidc

    companion = (
        "https://reef.example.com/api/auth/akb/sso/callback"
        "?redirect=%2Flogin%2Fsso-complete%3Fstate%3Dn-1%26next%3D%252Fissues"
    )
    allow(["https://reef.example.com"])
    monkeypatch.setattr(auth, "_require_keycloak", lambda: None)

    class FakeOIDC:
        async def consume_state(self, state):
            assert state == "state-1"
            return {"code_verifier": "verifier", "redirect_path": companion}

        async def exchange_code_for_tokens(self, code, verifier):
            assert (code, verifier) == ("code-1", "verifier")
            return {"id_token": "id-token"}

        async def verify_id_token(self, token):
            assert token == "id-token"
            return {"sub": "subject-1"}

    async def reject_membership(_claims):
        raise MembershipRequiredError()

    monkeypatch.setattr(keycloak_oidc, "get_keycloak_oidc", lambda: FakeOIDC())
    monkeypatch.setattr(auth, "login_with_keycloak_claims", reject_membership)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("akb.example.com", 443),
            "path": "/api/v1/auth/keycloak/callback",
            "query_string": b"code=code-1&state=state-1",
            "headers": [],
        }
    )

    response = await auth.keycloak_callback(request)

    location = response.headers["location"]
    parts = urllib.parse.urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}" == "https://reef.example.com"
    assert urllib.parse.parse_qs(parts.query)["sso_error"] == ["membership_required"]


@pytest.mark.asyncio
async def test_callback_returns_post_state_protocol_error_to_allowed_companion(
    allow, monkeypatch
):
    from app.services import keycloak_oidc

    companion = "https://reef.example.com/cb?redirect=%2Fdone"
    allow(["https://reef.example.com"])
    monkeypatch.setattr(auth, "_require_keycloak", lambda: None)

    class FakeOIDC:
        async def consume_state(self, _state):
            return {"code_verifier": "verifier", "redirect_path": companion}

        async def exchange_code_for_tokens(self, _code, _verifier):
            return {}

    monkeypatch.setattr(keycloak_oidc, "get_keycloak_oidc", lambda: FakeOIDC())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("akb.example.com", 443),
            "path": "/api/v1/auth/keycloak/callback",
            "query_string": b"code=code-1&state=state-1",
            "headers": [],
        }
    )

    response = await auth.keycloak_callback(request)

    location = response.headers["location"]
    parts = urllib.parse.urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}" == "https://reef.example.com"
    assert urllib.parse.parse_qs(parts.query)["sso_error"] == ["no_id_token"]
