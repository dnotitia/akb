"""Unit contract for provider-authoritative email domains (dnotitia/akb#529).

The authority map is installation config, validated fail-closed at load:
alias keys obey the provider contract (case-sensitive, lowercase-only,
`local` reserved), domains are exact-match only (no subdomain inheritance),
and the bearer projection paths (alias ``None``) stay inert.
"""

from __future__ import annotations

import pytest

from app import config as app_config
from app.config import AuthModeConfigurationError, Settings


def _settings(**overrides) -> Settings:
    # Standalone SSO remains an explicit legacy installation fixture.
    values = {
        "document_revision_backend": "bare_git",
        "auth_mode": "sso",
        "keycloak_enabled": True,
        **overrides,
    }
    return Settings.model_validate(values)


# ── defaults ──────────────────────────────────────────────────────────


def test_authority_map_defaults_to_empty() -> None:
    assert app_config.Settings.model_fields[
        "keycloak_authoritative_email_domains_by_provider"
    ].default_factory() == {}


def test_empty_map_resolves_nothing() -> None:
    loaded = _settings()

    assert loaded.authoritative_email_domains_for("workforce") == ()
    assert loaded.authoritative_email_domains_for(None) == ()
    assert loaded.authoritative_email_domains_for("local") == ()


# ── normalization ─────────────────────────────────────────────────────


def test_domains_normalize_case_trailing_dot_and_duplicates() -> None:
    loaded = _settings(
        keycloak_authoritative_email_domains_by_provider={
            "workforce": ["Example.COM.", "example.com", "sub.example.com"],
        }
    )

    assert loaded.keycloak_authoritative_email_domains_by_provider == {
        "workforce": ["example.com", "sub.example.com"]
    }


def test_subdomain_is_not_inherited() -> None:
    loaded = _settings(
        keycloak_authoritative_email_domains_by_provider={"workforce": ["example.com"]}
    )

    assert loaded.authoritative_email_domains_for("workforce") == ("example.com",)


def test_unknown_alias_resolves_empty() -> None:
    loaded = _settings(
        keycloak_authoritative_email_domains_by_provider={"workforce": ["example.com"]}
    )

    assert loaded.authoritative_email_domains_for("other") == ()


# ── fail-closed ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        {"local": ["example.com"]},
        {"Workforce": ["example.com"]},
        {"bad alias!": ["example.com"]},
        {123: ["example.com"]},
        {"workforce": []},
        {"workforce": ["*.example.com"]},
        {"workforce": ["singlelabel"]},
        {"workforce": ["exa mple.com"]},
        {"workforce": [""]},
        {"workforce": [".example.com"]},
        {"workforce": ["-leading.example.com"]},
        {"workforce": ["trailing-.example.com"]},
        {"workforce": ["toolonglabel" + "a" * 60 + ".example.com"]},
        {"workforce": [123]},
        {"workforce": ["example.com", 123]},
        {"workforce": "example.com"},
        ["example.com"],
    ],
)
def test_invalid_map_fails_load(raw: object) -> None:
    with pytest.raises((AuthModeConfigurationError, ValueError)):
        _settings(keycloak_authoritative_email_domains_by_provider=raw)


def test_none_map_normalizes_to_empty() -> None:
    loaded = _settings(keycloak_authoritative_email_domains_by_provider=None)

    assert loaded.keycloak_authoritative_email_domains_by_provider == {}


def test_punycode_expansion_past_dns_limit_is_rejected() -> None:
    # 63 "ü" characters: short pre-encoding, 252+ octets of xn-- wire form.
    with pytest.raises((AuthModeConfigurationError, ValueError)):
        _settings(
            keycloak_authoritative_email_domains_by_provider={
                "workforce": ["ü" * 63 + ".de"]
            }
        )


# ── domain matching helper ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("user@example.com", "example.com"),
        ("User@Example.COM", "example.com"),
        ("user@sub.example.com", None),
        ("user@other.com", None),
        ("not-an-email", None),
        ("user@outside.example@example.com", None),
        ("user@", None),
        ("@example.com", None),
        ("user@münchen.de", "xn--mnchen-3ya.de"),
        ("user@xn--mnchen-3ya.de", "xn--mnchen-3ya.de"),
    ],
)
def test_authority_domain_of_matches_exact_only(email: str, expected: str | None) -> None:
    from app.services.auth_service import _authority_domain_of

    assert _authority_domain_of(email, ("example.com", "xn--mnchen-3ya.de")) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [None, "local", "workforce", "other"])
@pytest.mark.parametrize("email", [None, "person@outside.example"])
@pytest.mark.parametrize("authority_map", [{}, {"workforce": ["corp.example"]}])
async def test_non_authority_browser_arrival_keeps_membership_refusal(
    monkeypatch, alias, email, authority_map
):
    from unittest.mock import AsyncMock, MagicMock

    from app.exceptions import MembershipRequiredError
    from app.services import auth_service

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.transaction.return_value = MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)
    )
    pool = MagicMock()
    pool.acquire.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock(return_value=False)
    )
    monkeypatch.setattr(auth_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(auth_service, "_bound_external_user", AsyncMock(return_value=None))
    arrival = AsyncMock()
    monkeypatch.setattr(auth_service, "record_arrival", arrival)
    monkeypatch.setattr(auth_service, "settings", _settings(
        keycloak_enrollment_mode="invite_only",
        keycloak_require_verified_email=True,
        keycloak_authoritative_email_domains_by_provider=authority_map,
    ))
    claims = {"iss": "https://id.example/realms/test", "sub": "arrived", "email_verified": False}
    if email is not None:
        claims["email"] = email

    with pytest.raises(MembershipRequiredError):
        await auth_service._resolve_or_provision_keycloak_user(claims, provider_alias=alias)

    arrival.assert_awaited_once_with(
        conn, issuer=claims["iss"], subject=claims["sub"], claims=claims
    )
