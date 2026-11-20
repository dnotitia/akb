"""The SSO callback answers a person, not a serializer.

A browser reaches this route by following a redirect, so whatever the route
returns IS the page. Raising rendered a serialized error object on a blank white
screen: an invited person waiting for approval, measured against a deployed
workspace, saw `{"message":"SSO sign-in failed",...}` and nothing else.

Two properties, and the second is a disclosure decision rather than a nicety:

  * every refusal answers with a redirect back to the product's sign-in page;
  * exactly one refusal is named there. Naming one tells a caller that this
    runtime verified their token and what it then decided. That is defensible
    for membership — to hold such a token they already authenticated at an
    upstream this workspace's owner registered — and it is not defensible for
    "your account is suspended", which is a statement about the person.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.exceptions import AuthenticationError

_STORAGE = tempfile.mkdtemp(prefix="sso-callback-page-")
object.__setattr__(settings, "git_storage_path", _STORAGE)

from app.api.routes import auth  # noqa: E402


def _location(response) -> str:
    return response.headers["location"]


def test_every_refusal_answers_with_the_sign_in_page():
    for reason in (None, "", "membership_required", "account_suspended",
                   "identity_conflict", "external_auth_disabled", "nonsense"):
        response = auth._sso_sign_in_failed(reason)
        assert response.status_code == 303, reason
        assert _location(response).startswith("/auth?sso_error="), reason


def test_only_membership_is_named_and_everything_else_is_generic():
    assert _location(auth._sso_sign_in_failed("membership_required")) == \
        "/auth?sso_error=membership_required"
    for reason in ("account_suspended", "identity_conflict", "credential_change_required",
                   "external_auth_disabled", "permission_denied", None, ""):
        assert _location(auth._sso_sign_in_failed(reason)) == "/auth?sso_error=sso_failed", reason


def test_the_named_set_is_exactly_one_and_it_is_membership():
    # Written as equality rather than membership so a second reason cannot be
    # added without this test saying so — the whole point is that adding one is
    # a decision about disclosure, not a refactor.
    assert auth._NAMEABLE_SSO_REFUSALS == frozenset({"membership_required"})


def test_the_reason_cannot_put_text_on_the_screen():
    """The query string is an allowlisted code, never an echoed message."""
    injected = "membership_required</a><script>alert(1)</script>"
    assert _location(auth._sso_sign_in_failed(injected)) == "/auth?sso_error=sso_failed"


@pytest.mark.parametrize(
    ("purpose", "location"),
    [
        ({}, "/auth?sso_error=sso_failed"),
        ({"purpose": "monitoring"}, "/admin/mcp-monitoring?auth_error=sign_in_failed"),
    ],
)
def test_callback_redirects_to_its_sign_in_page_when_exchange_fails(monkeypatch, purpose, location):
    state = {
        "client_id": settings.keycloak_client_id,
        "redirect_path": "/admin/mcp-monitoring" if purpose else "/",
        "provider_alias": "local",
        "code_verifier": "fixture-verifier",
        "nonce": "fixture-nonce",
        **purpose,
    }
    exchange = AsyncMock(side_effect=AuthenticationError("private exchange failure"))
    oidc = SimpleNamespace(consume_browser_state=AsyncMock(return_value=state), exchange_browser_code=exchange)
    monkeypatch.setattr(auth, "_require_sso_browser_session", lambda: None)
    monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: oidc)
    app = FastAPI()
    app.include_router(auth.router)

    with TestClient(app) as client:
        response = client.get("/auth/keycloak/callback?code=fixture-code&state=fixture-state", follow_redirects=False)

    assert response.status_code == 303
    assert _location(response) == location
    assert "private exchange failure" not in response.text
    exchange.assert_awaited_once_with("fixture-code", "fixture-verifier")


@pytest.mark.asyncio
async def test_the_projection_boundary_carries_the_reason_it_already_knew():
    from app.services.auth_service import ProjectionOutcome

    assert ProjectionOutcome(None).refusal_code is None
    assert ProjectionOutcome(None, "membership_required").refusal_code == "membership_required"
