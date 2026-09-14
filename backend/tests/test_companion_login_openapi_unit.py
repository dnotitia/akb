"""Public completion contract stays discoverable without changing credential handling."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import auth
from app.exceptions import AccountSuspendedError, AuthenticationError, ExternalIdentityConflictError, MembershipRequiredError

_PATH = "/api/v1/auth/sso/companion/complete"
_BODY = {"provider_alias": "workforce", "nonce": "n" * 43}
_HEADERS = [("Authorization", "Bearer access-token"), ("X-AKB-ID-Token", "id-token"),
            ("X-AKB-Login-Assertion", "signed-assertion")]


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/v1")
    return app


def test_openapi_declares_all_credentials_and_response_shapes(app):
    schema = app.openapi()
    operation = schema["paths"][_PATH]["post"]
    assert operation["security"] == [{"bearerAuth": []}]
    assert schema["components"]["securitySchemes"]["bearerAuth"] == {"type": "http", "scheme": "bearer"}
    headers = {item["name"]: item for item in operation["parameters"]}
    assert set(headers) == {"X-AKB-ID-Token", "X-AKB-Login-Assertion"}
    assert all(item["required"] and item["in"] == "header" for item in headers.values())
    models = schema["components"]["schemas"]
    def response_model(status):
        ref = operation["responses"][str(status)]["content"]["application/json"]["schema"]["$ref"]
        return models[ref.rsplit("/", 1)[1]]
    success = response_model(200)
    assert success["required"] == ["user"]
    user = models[success["properties"]["user"]["$ref"].rsplit("/", 1)[1]]
    assert set(user["required"]) == {"id", "username", "email", "display_name", "is_admin"}
    for status in (401, 403, 409):
        assert set(response_model(status)["required"]) == {"message", "error", "detail", "code"}
    assert response_model(401)["properties"]["code"]["const"] == "authentication_failed"
    assert response_model(403)["properties"]["code"]["enum"] == ["membership_required", "account_suspended"]
    assert response_model(409)["properties"]["code"]["const"] == "identity_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_missing_or_duplicate_credentials_still_return_sanitized_401(app, monkeypatch, index, duplicate):
    complete = AsyncMock()
    monkeypatch.setattr(auth, "complete_companion_login", complete)
    headers = _HEADERS + [_HEADERS[index]] if duplicate else [h for i, h in enumerate(_HEADERS) if i != index]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example") as client:
        response = await client.post(_PATH, json=_BODY, headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_failed"
    assert response.headers["cache-control"] == "no-store"
    assert all(value not in response.text for _, value in _HEADERS)
    complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [AuthenticationError("private diagnostics"), MembershipRequiredError(),
                                       AccountSuspendedError(), ExternalIdentityConflictError()])
async def test_error_contract_preserves_status_and_sanitization(app, monkeypatch, error):
    complete = AsyncMock(side_effect=error)
    monkeypatch.setattr(auth, "complete_companion_login", complete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example") as client:
        response = await client.post(_PATH, json=_BODY, headers=_HEADERS)
    assert response.status_code == error.status_code
    assert response.json()["code"] == (error.code or "authentication_failed")
    assert "private diagnostics" not in response.text
    assert response.headers["cache-control"] == "no-store"
    complete.assert_awaited_once()
