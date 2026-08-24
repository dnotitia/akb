"""Ordinary SSO browser routes never return a Keycloak or AKB user token."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from starlette.requests import Request
import pytest

from app.api.routes import auth
from app.config import settings
from app.exceptions import AuthenticationError
from app.services.auth_service import AuthenticatedUser, ProjectionOutcome
from app.services.auth_verifier_profiles import VerifiedPrincipal
from app.services.keycloak_oidc import BrowserAuthorizationRequest
from app.services.sso_browser_session_service import (
    IssuedSsoBrowserSession,
    RevokedSsoBrowserSession,
)
from app.sso.models import ProviderReadback


def _request(
    *,
    cookie: str = "",
    csrf: str = "",
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if csrf:
        headers.append((b"x-akb-csrf", csrf.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/keycloak/callback",
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("akb.example.com", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://id.example", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web", raising=False)
    monkeypatch.setattr(settings, "api_oauth_audience", "https://akb.example.com/api", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    monkeypatch.setattr(auth, "sso_browser_session_ready", lambda: True)


def _provider(alias: str = "workforce", state: str = "enabled") -> ProviderReadback:
    return ProviderReadback(
        provider_type="keycloak-oidc",
        alias=alias,
        display_name="Company SSO",
        state=state,  # type: ignore[arg-type]
        enabled=state == "enabled",
        issuer="https://upstream.example/realms/workforce",
        discovery_url=("https://upstream.example/realms/workforce/.well-known/openid-configuration"),
        client_id="akb-broker",
        client_secret_configured=True,
        redirect_uri=("https://id.example/realms/akb/broker/workforce/endpoint"),
        post_logout_redirect_uri=(
            "https://id.example/realms/akb/broker/workforce/endpoint/logout_response"
        ),
        supports_logout=True,
        supports_identity_migration=True,
    )


class _Control:
    control_mode = "direct"

    def __init__(self, providers=(_provider(),)):
        self.providers = providers
        self.calls: list[dict[str, bool]] = []

    async def list_providers(self, **kwargs):
        self.calls.append(kwargs)
        return self.providers


@pytest.mark.asyncio
async def test_enabled_provider_login_sets_only_a_bound_transient_cookie(monkeypatch):
    _configure(monkeypatch)
    control = _Control()

    class OIDC:
        async def begin_browser_login(self, redirect_path, *, provider_alias):
            assert (redirect_path, provider_alias) == ("/vaults?one=1", "workforce")
            return BrowserAuthorizationRequest(
                location="https://id.example/authorize?state=server-owned",
                browser_binding="browser-binding-value-that-is-long-enough",
            )

    monkeypatch.setattr(auth, "get_keycloak_provider_control", lambda: control)
    monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: OIDC())

    response = await auth.sso_provider_login("workforce", "/vaults?one=1")

    assert response.status_code == 303
    assert response.headers["location"] == ("https://id.example/authorize?state=server-owned")
    cookies = response.headers.getlist("set-cookie")
    assert len(cookies) == 1
    assert cookies[0].startswith("__Host-akb_sso_oidc_binding=")
    assert "HttpOnly" in cookies[0]
    assert "Secure" in cookies[0]
    assert "Path=/" in cookies[0]
    assert "Domain=" not in cookies[0]
    assert control.calls == [{"allow_stale": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("alias", "providers"),
    [
        ("../escape", (_provider(),)),
        ("workforce", (_provider(state="configured_disabled"),)),
        ("missing", (_provider(),)),
    ],
)
async def test_unmanaged_or_disabled_provider_cannot_start_login(
    monkeypatch,
    alias,
    providers,
):
    _configure(monkeypatch)
    monkeypatch.setattr(auth, "get_keycloak_provider_control", lambda: _Control(providers))

    with pytest.raises(HTTPException) as captured:
        await auth.sso_provider_login(alias, "/")

    assert captured.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/path",
        "//evil.example/path",
        "/\\evil.example/path",
        "/ok#fragment",
        "relative",
    ],
)
async def test_login_rejects_non_relative_or_fragment_redirects(monkeypatch, redirect):
    _configure(monkeypatch)

    with pytest.raises(AuthenticationError, match="redirect"):
        await auth.sso_provider_login("workforce", redirect)


@pytest.mark.asyncio
async def test_callback_projects_access_token_but_returns_only_opaque_cookies(
    monkeypatch,
):
    _configure(monkeypatch)
    principal = VerifiedPrincipal(
        profile_id="keycloak-access-v1",
        issuer="https://id.example/realms/akb",
        subject="subject-1",
        credential_type="access_token",
        claims={
            "iss": "https://id.example/realms/akb",
            "sub": "subject-1",
            "sid": "sid-1",
            "identity_provider": "workforce",
            "scope": "openid profile",
            "iat": 1,
            "exp": 2,
        },
        audience="https://akb.example.com/api",
    )
    user = AuthenticatedUser(
        user_id="00000000-0000-0000-0000-000000000001",
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        is_admin=False,
        auth_method="oauth",
    )
    token_response = {
        "token_type": "Bearer",
        "access_token": "keycloak-access-secret",
        "refresh_token": "keycloak-refresh-secret",
        "id_token": "keycloak-id-secret",
        "refresh_expires_in": 3600,
    }

    class OIDC:
        async def consume_browser_state(self, state, browser_binding):
            assert (state, browser_binding) == (
                "state-1",
                "browser-binding-value-that-is-long-enough",
            )
            return {
                "redirect_path": "/vaults?selected=one",
                "provider_alias": "workforce",
                "client_id": "akb-web",
                "code_verifier": "verifier-value",
                "nonce": "nonce-value-that-is-long-enough",
            }

        async def exchange_browser_code(self, code, verifier):
            assert (code, verifier) == ("code-1", "verifier-value")
            return token_response

        async def verify_access_token(self, token, audience, *, route_profile):
            assert (token, audience, route_profile) == (
                "keycloak-access-secret",
                "https://akb.example.com/api",
                "api",
            )
            return principal

        async def verify_browser_id_token(
            self,
            token,
            *,
            expected_nonce,
            access_token,
            expected_provider_alias,
        ):
            assert (token, expected_nonce, access_token, expected_provider_alias) == (
                "keycloak-id-secret",
                "nonce-value-that-is-long-enough",
                "keycloak-access-secret",
                "workforce",
            )
            return {
                "iss": principal.issuer,
                "sub": principal.subject,
                "sid": "sid-1",
                "identity_provider": "workforce",
            }

    captured: dict[str, object] = {}

    async def project(value):
        assert value is principal
        # The boundary carries a reason alongside the account now, so the callback
        # can tell "not a member" apart from every other refusal. Success carries
        # none.
        return ProjectionOutcome(user)

    async def create(value, verified, id_claims, tokens):
        captured.update(
            value=value,
            verified=verified,
            id_claims=id_claims,
            tokens=tokens,
        )
        return IssuedSsoBrowserSession(
            token="opaque-session-value-that-is-long-enough",
            csrf_token="opaque-csrf-value-that-is-long-enough",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: OIDC())
    monkeypatch.setattr(auth, "project_verified_principal_with_reason", project)
    monkeypatch.setattr(auth, "create_sso_browser_session", create)
    request = _request(cookie="__Host-akb_sso_oidc_binding=browser-binding-value-that-is-long-enough")

    response = await auth.keycloak_callback(
        request,
        code="code-1",
        state="state-1",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/vaults?selected=one"
    cookies = response.headers.getlist("set-cookie")
    wire = "\n".join(cookies)
    assert "__Host-akb_sso_session=opaque-session-value-that-is-long-enough" in wire
    assert "__Host-akb_sso_csrf=opaque-csrf-value-that-is-long-enough" in wire
    session_cookie = next(value for value in cookies if value.startswith("__Host-akb_sso_session="))
    assert "Path=/" in session_cookie
    assert "Path=/api/v1" not in session_cookie
    assert "Secure" in session_cookie
    assert "Domain=" not in session_cookie
    assert "keycloak-access-secret" not in wire
    assert "keycloak-refresh-secret" not in wire
    assert "keycloak-id-secret" not in wire
    assert captured["tokens"] is token_response


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"identity_provider": "another-provider"},
        {"identity_provider": "Workforce"},
    ],
)
def test_signed_provider_claim_must_match_selected_alias(claims):
    with pytest.raises(AuthenticationError, match="sign-in failed"):
        auth._require_signed_provider(claims, "workforce")  # noqa: SLF001


@pytest.mark.asyncio
async def test_unbound_or_replayed_callback_fails_before_token_exchange(monkeypatch):
    _configure(monkeypatch)

    class OIDC:
        async def consume_browser_state(self, _state, _binding):
            return None

        async def exchange_browser_code(self, *_args):
            raise AssertionError("unbound state must fail before token exchange")

    monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: OIDC())
    # The point of this case is unchanged and is asserted by OIDC.exchange_browser_code
    # above: an unbound or replayed state must fail BEFORE the code is exchanged.
    # What changed is the shape of the failure. The callback is reached by a
    # browser following a redirect, so raising rendered a serialized error object
    # as the page; it now answers with the product's sign-in page instead.
    response = await auth.keycloak_callback(
        _request(),
        code="code-1",
        state="state-1",
    )

    assert response.status_code == 303
    # And it says nothing about why. A replayed state is not a person waiting for
    # admission, and the only named refusal is the one that helps them.
    assert response.headers["location"] == "/auth?sso_error=sso_failed"


@pytest.mark.asyncio
async def test_legacy_exchange_is_permanently_gone_in_sso_mode(monkeypatch):
    _configure(monkeypatch)

    with pytest.raises(HTTPException) as captured:
        await auth.keycloak_exchange()

    assert captured.value.status_code == 410
    assert "does not issue an AKB user JWT" in captured.value.detail


@pytest.mark.asyncio
async def test_logout_deletes_local_handle_revokes_keycloak_and_clears_cookies(
    monkeypatch,
):
    _configure(monkeypatch)
    calls: list[tuple[str, ...]] = []

    async def revoke(token, csrf_cookie, csrf_header):
        calls.append((token, csrf_cookie, csrf_header))
        return RevokedSsoBrowserSession(
            refresh_token="refresh-secret",
        )

    class OIDC:
        async def revoke_browser_refresh_token(self, token):
            calls.append(("remote", token))
            return True

        def ordinary_logout_url(self, redirect):
            assert redirect == "https://akb.example.com/auth"
            return "https://id.example/logout?opaque=params"

    monkeypatch.setattr(auth, "revoke_sso_browser_session", revoke)
    monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: OIDC())
    response = await auth.sso_browser_logout(
        _request(
            cookie=(
                "__Host-akb_sso_session=session-value-that-is-long-enough; "
                "__Host-akb_sso_csrf=csrf-value-that-is-long-enough"
            ),
            csrf="csrf-value-that-is-long-enough",
        )
    )

    assert response.status_code == 200
    assert b"refresh-secret" not in response.body
    assert calls == [
        (
            "session-value-that-is-long-enough",
            "csrf-value-that-is-long-enough",
            "csrf-value-that-is-long-enough",
        ),
        ("remote", "refresh-secret"),
    ]
    wire = "\n".join(response.headers.getlist("set-cookie"))
    assert "__Host-akb_sso_session=" in wire and "Max-Age=0" in wire
    assert "__Host-akb_sso_csrf=" in wire


@pytest.mark.asyncio
async def test_verified_backchannel_logout_uses_only_exact_issuer_sid_subject(
    monkeypatch,
):
    _configure(monkeypatch)
    selected: list[tuple[str, str, str | None, int, int]] = []
    now = int(datetime.now(timezone.utc).timestamp())

    class OIDC:
        async def verify_backchannel_logout_token(self, token):
            assert token == "signed-logout-token"
            return {
                "iss": "https://id.example/realms/akb",
                "sid": "sid-1",
                "sub": "subject-1",
                "iat": now,
                "exp": now + 300,
            }

    async def revoke(*, issuer, sid, subject, issued_at, expires_at):
        selected.append((issuer, sid, subject, issued_at, expires_at))
        return 2

    monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: OIDC())
    monkeypatch.setattr(auth, "revoke_sso_browser_sessions_from_logout_token", revoke)

    response = await auth.keycloak_backchannel_logout("signed-logout-token")

    assert response.status_code == 204
    assert selected == [("https://id.example/realms/akb", "sid-1", "subject-1", now, now + 300)]


@pytest.mark.asyncio
async def test_invalid_backchannel_logout_token_is_a_bounded_400(monkeypatch):
    _configure(monkeypatch)

    class OIDC:
        async def verify_backchannel_logout_token(self, _token):
            raise AuthenticationError("signature detail must not escape")

    monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: OIDC())

    with pytest.raises(HTTPException) as captured:
        await auth.keycloak_backchannel_logout("invalid-logout-token")

    assert captured.value.status_code == 400
    assert captured.value.detail == {
        "message": "Invalid back-channel logout token",
        "code": "invalid_logout_token",
    }
