"""Fail-closed Phase 1 browser OIDC route gates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes import auth
from app.config import settings
from app.exceptions import AKBError


def _callback_request() -> Request:
    return Request(
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


def _browser_route_calls() -> list[tuple[str, Callable[[], Awaitable[object]]]]:
    return [
        ("login", lambda: auth.keycloak_login("/")),
        ("callback", lambda: auth.keycloak_callback(_callback_request())),
        (
            "exchange",
            auth.keycloak_exchange,
        ),
        ("logout", lambda: auth.keycloak_logout()),
    ]


def _forbid_oidc_calls(monkeypatch) -> None:
    from app.services import auth_service, keycloak_oidc

    def forbidden(*_args, **_kwargs):
        raise AssertionError("staged browser route must not call OIDC/session code")

    monkeypatch.setattr(keycloak_oidc, "get_keycloak_oidc", forbidden)
    monkeypatch.setattr(keycloak_oidc, "issue_exchange_code", forbidden, raising=False)
    monkeypatch.setattr(keycloak_oidc, "redeem_exchange_code", forbidden, raising=False)
    monkeypatch.setattr(
        auth_service,
        "login_with_keycloak_claims",
        forbidden,
        raising=False,
    )
    monkeypatch.setattr(auth, "login_with_keycloak_claims", forbidden, raising=False)
    monkeypatch.setattr(auth_service, "create_jwt", forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,call", _browser_route_calls())
async def test_local_mode_hides_human_keycloak_routes_before_oidc_calls(
    monkeypatch,
    name,
    call,
):
    del name
    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    _forbid_oidc_calls(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await call()

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("name,call", _browser_route_calls())
async def test_sso_browser_routes_are_stable_503_before_mint_or_redeem(
    monkeypatch,
    name,
    call,
):
    del name
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    _forbid_oidc_calls(monkeypatch)

    with pytest.raises(AKBError) as exc_info:
        await call()

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "browser_session_not_ready"
