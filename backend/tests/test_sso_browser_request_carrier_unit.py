"""Request-context selection for bearer versus opaque SSO browser sessions."""

from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import Request
import pytest

from app.api import deps
from app.config import settings
from app.exceptions import AuthenticationError, ForbiddenError
from app.services.auth_service import AuthenticatedUser


@pytest.fixture(autouse=True)
def _https_cookie_profile(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)


def _request(
    *,
    method: str = "GET",
    authorization: str | None = None,
    cookie: str | None = None,
    csrf_header: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    if csrf_header is not None:
        headers.append((b"x-akb-csrf", csrf_header.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/v1/auth/me",
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("akb.example.com", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def _actor(method: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="00000000-0000-0000-0000-000000000001",
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        is_admin=False,
        auth_method=method,
    )


@pytest.mark.asyncio
async def test_explicit_bearer_never_falls_back_to_a_valid_cookie(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    bearer_calls: list[str] = []

    async def reject_bearer(value):
        bearer_calls.append(value)
        return None

    async def forbidden_cookie(*_args, **_kwargs):
        raise AssertionError("explicit bearer must own the request")

    monkeypatch.setattr(deps, "resolve_rest_user_authorization", reject_bearer)
    monkeypatch.setattr(deps, "resolve_sso_browser_session", forbidden_cookie)
    request = _request(
        authorization="Bearer rejected-token",
        cookie="__Host-akb_sso_session=valid-cookie-token-that-is-long-enough",
    )

    with pytest.raises(HTTPException) as captured:
        await deps.get_current_user(request, None)

    assert captured.value.status_code == 401
    assert bearer_calls == ["Bearer rejected-token"]


@pytest.mark.asyncio
async def test_sso_cookie_is_a_separate_read_carrier_when_bearer_is_absent(
    monkeypatch,
):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(deps, "sso_browser_session_ready", lambda: True)
    actor = _actor("browser_session")
    calls: list[tuple[object, ...]] = []

    async def resolve(token, *, require_csrf, csrf_cookie, csrf_header):
        calls.append((token, require_csrf, csrf_cookie, csrf_header))
        return actor

    async def forbidden_bearer(_value):
        raise AssertionError("no bearer resolver call without an Authorization header")

    monkeypatch.setattr(deps, "resolve_sso_browser_session", resolve)
    monkeypatch.setattr(deps, "resolve_rest_user_authorization", forbidden_bearer)
    request = _request(cookie="__Host-akb_sso_session=session-cookie-token-that-is-long-enough")

    assert await deps.get_current_user(request, None) is actor
    assert calls == [("session-cookie-token-that-is-long-enough", False, "", "")]


@pytest.mark.asyncio
async def test_cookie_mutation_requires_double_submit_csrf(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(deps, "sso_browser_session_ready", lambda: True)
    actor = _actor("browser_session")
    calls: list[tuple[object, ...]] = []

    async def resolve(token, *, require_csrf, csrf_cookie, csrf_header):
        calls.append((token, require_csrf, csrf_cookie, csrf_header))
        return actor

    monkeypatch.setattr(deps, "resolve_sso_browser_session", resolve)
    request = _request(
        method="POST",
        cookie=(
            "__Host-akb_sso_session=session-cookie-token-that-is-long-enough; "
            "__Host-akb_sso_csrf=csrf-cookie-token-that-is-long-enough"
        ),
        csrf_header="csrf-cookie-token-that-is-long-enough",
    )

    assert await deps.get_current_user(request, None) is actor
    assert calls == [
        (
            "session-cookie-token-that-is-long-enough",
            True,
            "csrf-cookie-token-that-is-long-enough",
            "csrf-cookie-token-that-is-long-enough",
        )
    ]


@pytest.mark.asyncio
async def test_invalid_cookie_auth_is_401_but_csrf_denial_remains_403(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(deps, "sso_browser_session_ready", lambda: True)

    async def invalid(*_args, **_kwargs):
        raise AuthenticationError()

    monkeypatch.setattr(deps, "resolve_sso_browser_session", invalid)
    with pytest.raises(HTTPException) as captured:
        await deps.get_current_user(
            _request(cookie="__Host-akb_sso_session=session-cookie-token-that-is-long-enough"),
            None,
        )
    assert captured.value.status_code == 401

    async def csrf_denied(*_args, **_kwargs):
        raise ForbiddenError("Invalid SSO CSRF token")

    monkeypatch.setattr(deps, "resolve_sso_browser_session", csrf_denied)
    with pytest.raises(ForbiddenError):
        await deps.get_current_user(
            _request(
                method="POST",
                cookie="__Host-akb_sso_session=session-cookie-token-that-is-long-enough",
            ),
            None,
        )


@pytest.mark.asyncio
async def test_local_mode_never_interprets_an_sso_cookie(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)

    async def forbidden_cookie(*_args, **_kwargs):
        raise AssertionError("local mode must not inspect an SSO browser cookie")

    monkeypatch.setattr(deps, "resolve_sso_browser_session", forbidden_cookie)
    with pytest.raises(HTTPException) as captured:
        await deps.get_current_user(
            _request(cookie="__Host-akb_sso_session=session-cookie-token-that-is-long-enough"),
            None,
        )
    assert captured.value.status_code == 401


@pytest.mark.asyncio
async def test_optional_auth_uses_cookie_only_without_an_explicit_bearer(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(deps, "sso_browser_session_ready", lambda: True)
    actor = _actor("browser_session")

    async def resolve(*_args, **_kwargs):
        return actor

    monkeypatch.setattr(deps, "resolve_sso_browser_session", resolve)
    assert (
        await deps.get_optional_user(
            _request(cookie="__Host-akb_sso_session=session-cookie-token-that-is-long-enough"),
            None,
        )
        is actor
    )

    async def reject_bearer(_value):
        return None

    async def forbidden_cookie(*_args, **_kwargs):
        raise AssertionError("explicit bearer rejection must not fall through")

    monkeypatch.setattr(deps, "resolve_rest_user_authorization", reject_bearer)
    monkeypatch.setattr(deps, "resolve_sso_browser_session", forbidden_cookie)
    assert (
        await deps.get_optional_user(
            _request(
                authorization="Bearer rejected",
                cookie="__Host-akb_sso_session=session-cookie-token-that-is-long-enough",
            ),
            None,
        )
        is None
    )
