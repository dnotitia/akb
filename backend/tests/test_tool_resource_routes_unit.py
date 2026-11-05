"""Signed tool credentials enter only the eight declared file controls."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx
import pytest

from app.api import deps, file_write_context
from app.api.routes import assets, files
from app.config import settings
from app.exceptions import AKBError, ForbiddenError
from app.services import auth_service, keycloak_oidc
from app.services.auth_service import AuthenticatedUser
from tests.test_auth_verifier_profiles_unit import _configure_keycloak, _mint_keycloak_token, rsa_keypair  # noqa: F401

FILE_ID = "11111111-1111-4111-8111-111111111111"
ROWS = [
    ("POST", "/files/team/upload?filename=x", "akb:vault:write", "reserve"),
    ("POST", f"/files/team/{FILE_ID}/confirm", "akb:vault:write", "confirm"),
    ("POST", f"/files/team/{FILE_ID}/replace?content_hash=abc", "akb:vault:write", "replace"),
    ("POST", f"/files/team/{FILE_ID}/replace/replacement/confirm?content_hash=abc", "akb:vault:write", "replace_confirm"),
    ("GET", f"/files/team/{FILE_ID}/download", "akb:vault:read", "download"),
    ("DELETE", f"/files/team/{FILE_ID}", "akb:vault:write", "delete"),
    ("POST", "/assets/team?filename=x.png", "akb:vault:write", "image"),
    ("DELETE", f"/assets/team/{FILE_ID}", "akb:vault:write", "discard"),
]


@pytest.fixture
def boundary(monkeypatch, rsa_keypair):  # noqa: F811
    issuer, _, resource = _configure_keycloak(monkeypatch)
    service = keycloak_oidc.KeycloakOIDC()
    service._jwks = {"keys": [rsa_keypair["jwk"]]}
    monkeypatch.setattr(keycloak_oidc, "get_keycloak_oidc", lambda: service)
    calls, principals = [], []

    async def project(principal, **_kwargs):
        principals.append(principal)
        return AuthenticatedUser(
            user_id=str(uuid.uuid4()), username="alice", email="alice@example.com", display_name=None,
            is_admin=principal.claims.get("test_admin", False), auth_method="oauth",
            oauth_scopes=principal.claims["scope"].split(), oauth_audience=principal.audience,
        )

    monkeypatch.setattr(auth_service, "project_verified_principal", project)

    def token(scope, **claims):
        return _mint_keycloak_token(rsa_keypair, issuer=issuer, audience=resource,
                                   claim_overrides={"azp": "native-client", "scope": scope, **claims})

    async def access(*_args, **_kwargs):
        return {"vault_id": uuid.uuid4(), "role": "writer"}

    monkeypatch.setattr(file_write_context, "check_vault_access", access)
    monkeypatch.setattr(files, "check_vault_access", access)
    monkeypatch.setattr(assets, "check_vault_access", access)

    def stub(name):
        async def execute(*_args, **_kwargs):
            calls.append(name)
            return {"ok": name}
        return execute

    for method, label in [("initiate_upload", "reserve"), ("confirm_upload", "confirm"),
                          ("initiate_replace", "replace"), ("confirm_replace", "replace_confirm"),
                          ("get_download_url", "download"), ("delete", "delete")]:
        monkeypatch.setattr(files.file_service, method, stub(label))
    monkeypatch.setattr(assets.asset_service, "create_image_asset", stub("image"))

    @asynccontextmanager
    async def transaction():
        yield

    connection = SimpleNamespace(transaction=transaction)

    @asynccontextmanager
    async def acquire():
        yield connection

    async def pool():
        return SimpleNamespace(acquire=acquire)

    async def find_owned(*_args, **_kwargs):
        calls.append("discard")
        return None

    monkeypatch.setattr(assets, "get_pool", pool)
    monkeypatch.setattr(assets.vault_files_repo, "find_owned_attachment_for_discard", find_owned)
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1")
    app.include_router(assets.router, prefix="/api/v1")

    @app.exception_handler(AKBError)
    async def error(_request, exc):
        return JSONResponse({"code": exc.code, "error": exc.message}, status_code=exc.status_code)

    return SimpleNamespace(app=app, token=token, calls=calls, principals=principals)


@pytest.mark.parametrize("mode", ["local", "sso"])
@pytest.mark.parametrize("method,path,scope,label", ROWS)
def test_exact_eight_routes_require_independent_scope_before_service(boundary, monkeypatch, mode, method, path, scope, label):
    monkeypatch.setattr(settings, "auth_mode", mode)
    client = TestClient(boundary.app)
    wrong = "akb:vault:read" if scope.endswith("write") else "akb:vault:write"
    denied = client.request(method, "/api/v1" + path, content=b"image", headers={"Authorization": "Bearer " + boundary.token(wrong)})
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["code"] == "insufficient_scope"
    assert boundary.calls == []
    accepted = client.request(method, "/api/v1" + path, content=b"image", headers={"Authorization": "Bearer " + boundary.token(scope)})
    assert accepted.status_code in {200, 201}, accepted.text
    assert boundary.calls == [label]


@pytest.mark.parametrize("method,path,scope,label", ROWS)
def test_scope_never_replaces_vault_authority(boundary, monkeypatch, method, path, scope, label):
    async def deny(*_args, **_kwargs):
        raise ForbiddenError("Vault authority required")

    monkeypatch.setattr(file_write_context, "check_vault_access", deny)
    monkeypatch.setattr(files, "check_vault_access", deny)
    response = TestClient(boundary.app).request(method, "/api/v1" + path, content=b"image",
                                               headers={"Authorization": "Bearer " + boundary.token(scope)})
    assert response.status_code == 403
    assert boundary.calls == []


@pytest.mark.asyncio
async def test_image_scope_denial_does_not_consume_body(boundary):
    consumed = 0

    async def body():
        nonlocal consumed
        consumed += 1
        yield b"image"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary.app), base_url="http://test") as client:
        response = await client.post("/api/v1/assets/team?filename=x.png", content=body(),
                                     headers={"Authorization": "Bearer " + boundary.token("akb:vault:read")})
    assert response.status_code == 403
    assert consumed == 0
    assert boundary.calls == []


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("mode", ["local", "sso"])
def test_tool_credential_cannot_enter_other_rest_even_for_admin(boundary, monkeypatch, admin, mode):
    from app.main import app

    monkeypatch.setattr(settings, "auth_mode", mode)
    client = TestClient(app)
    token = boundary.token("akb:vault:read akb:vault:write", test_admin=admin)
    unlisted = [
        ("POST", f"/files/team/{FILE_ID}/cancel-upload"), ("GET", "/files/team"),
        ("GET", f"/files/team/{FILE_ID}"), ("GET", "/files/team/body-placements"),
        ("GET", "/assets/team/policy"), ("POST", f"/assets/team/from-file/{FILE_ID}"),
        ("GET", "/vaults"), ("GET", "/tables/team"),
        ("GET", f"/assets/team/{FILE_ID}/metadata"),
        ("GET", f"/apps/{FILE_ID}/credentials"), ("GET", "/admin/sso/operation-requests"),
        ("POST", "/app/authorize"), ("GET", "/users/search?q=alice"), ("GET", "/auth/me"),
        ("GET", "/auth/tokens"), ("POST", "/admin/reconcile-roles"),
        ("GET", "/my/account/lifecycle"),
    ]
    for method, path in unlisted:
        response = client.request(method, "/api/v1" + path, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 401, (path, response.text)
    assert boundary.principals == []
    assert boundary.calls == []


@pytest.mark.parametrize("claim", [{"aud": ["https://akb.example.com/mcp", "https://akb.example.com/api"]},
                                    {"azp": "unapproved-client"}, {"typ": "ID"}, {"aud": ["https://akb.example.com/mcp"]}])
def test_invalid_selected_tool_verifier_never_falls_back_to_api_or_cookie(boundary, monkeypatch, claim):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Invalid bearer must not try another verifier or browser cookie")

    monkeypatch.setattr(auth_service, "_verify_sso_rest_bearer", forbidden)
    monkeypatch.setattr(deps, "_resolve_cookie_user", forbidden)
    token = boundary.token("akb:vault:write", **claim)
    if claim == {"aud": ["https://akb.example.com/mcp"]}:
        token = token[:-3] + "bad"
    client = TestClient(boundary.app)
    client.cookies.set("session", "valid-cookie")
    response = client.post("/api/v1/files/team/upload?filename=x", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert boundary.principals == []
    assert boundary.calls == []


def test_only_eight_routes_install_the_tool_credential_dependency(boundary):
    from app.api.deps import ToolControlUser
    from app.services.tool_resource_policy import REST_OPERATION_SCOPES

    found = []
    for route in boundary.app.routes:
        for dependency in getattr(getattr(route, "dependant", None), "dependencies", []):
            if isinstance(dependency.call, ToolControlUser):
                found.append(dependency.call.operation)
    assert len(found) == 8
    assert set(found) == set(REST_OPERATION_SCOPES)


def test_tool_bearer_never_substitutes_for_an_upload_capability(boundary):
    response = TestClient(boundary.app).put("/api/v1/files/upload/!", content=b"image",
                                          headers={"Authorization": "Bearer " + boundary.token("akb:vault:write")})
    assert response.status_code == 404
    assert boundary.principals == []
    assert boundary.calls == []
