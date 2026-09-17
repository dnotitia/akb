"""Product-admin browser callback failures return a recoverable page."""

from __future__ import annotations

import inspect
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.exceptions import ForbiddenError

_STORAGE = tempfile.mkdtemp(prefix="admin-callback-page-")
object.__setattr__(settings, "git_storage_path", _STORAGE)

from app.api.routes import admin_auth  # noqa: E402


def _configure_admin_sso(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
    monkeypatch.setattr(settings, "keycloak_admin_client_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://akb.example", raising=False)


def _callback_client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_auth.router, prefix="/api/v1")
    client = TestClient(app)
    client.cookies.set(
        "__Host-akb_admin_oidc_binding",
        "browser-binding-value-that-is-long-enough",
        path="/",
    )
    return client


def _assert_recoverable_failure(response) -> None:
    assert response.status_code == 303
    assert response.headers["location"] == "/admin?auth_error=sign_in_failed"
    cookies = response.headers.get_list("set-cookie")
    assert any(
        value.startswith("__Host-akb_admin_oidc_binding=") and "Max-Age=0" in value and "Path=/" in value
        for value in cookies
    )
    assert "Product-admin sign-in failed" not in response.text


def test_expired_admin_state_returns_to_the_admin_page(monkeypatch, caplog) -> None:
    class ExpiredOIDC:
        async def consume_admin_state(self, state: str, browser_binding: str):
            return None

    _configure_admin_sso(monkeypatch)
    monkeypatch.setattr(admin_auth, "get_keycloak_oidc", lambda: ExpiredOIDC())

    response = _callback_client().get(
        "/api/v1/admin/auth/keycloak/callback",
        params={"code": "one-time-code", "state": "expired-state"},
        follow_redirects=False,
    )

    _assert_recoverable_failure(response)
    assert "state_missing_or_expired" in caplog.text
    assert "expired-state" not in caplog.text


def test_downstream_admin_refusal_returns_to_the_admin_page(monkeypatch) -> None:
    class RefusedOIDC:
        async def consume_admin_state(self, state: str, browser_binding: str):
            return {"code_verifier": "verifier", "nonce": "nonce"}

        async def exchange_admin_code(self, code: str, verifier: str):
            raise ForbiddenError("sensitive internal refusal")

    _configure_admin_sso(monkeypatch)
    monkeypatch.setattr(admin_auth, "get_keycloak_oidc", lambda: RefusedOIDC())

    response = _callback_client().get(
        "/api/v1/admin/auth/keycloak/callback",
        params={"code": "one-time-code", "state": "one-time-state"},
        follow_redirects=False,
    )

    _assert_recoverable_failure(response)
    assert "sensitive internal refusal" not in response.text


def test_admin_callback_never_raises_application_refusals() -> None:
    source = inspect.getsource(admin_auth.admin_keycloak_callback)
    assert "raise AuthenticationError" not in source
    assert "except AKBError" in source
