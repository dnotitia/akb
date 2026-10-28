"""Real PAT/session authority at the HTTP migration boundary.

The PostgreSQL credential store and route dependencies are real. Provider
readback is an in-process fixture; this suite does not claim Keycloak proof.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest

from app.api.routes import admin_sso
from app.config import settings
from app.exceptions import AKBError
from app.services import admin_auth_service, auth_service
from tests.test_admin_auth_postgres import _SSO_SESSION_EPOCH, _fresh_database
from tests.test_admin_sso_routes_unit import Control


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("operation", ["preflight", "apply", "rollback"])
async def test_real_provider_service_pat_cannot_authorize_identity_migration(
    monkeypatch, operation,
):
    async with _fresh_database() as pool:
        async def get_test_pool():
            return pool

        monkeypatch.setattr(auth_service, "get_pool", get_test_pool)
        monkeypatch.setattr(admin_auth_service, "get_pool", get_test_pool)
        monkeypatch.setattr(settings, "auth_mode", "sso")
        monkeypatch.setattr(settings, "auth_runtime_generation", 1)
        monkeypatch.setattr(settings, "sso_session_epoch", _SSO_SESSION_EPOCH)
        monkeypatch.setattr(settings, "public_base_url", "https://akb.example")
        monkeypatch.setattr(admin_sso, "get_keycloak_provider_control", Control)

        uid, tid = uuid.uuid4(), uuid.uuid4()
        raw, token_hash, token_prefix = auth_service.generate_pat("service")
        await pool.execute(
            """INSERT INTO users (
                id, username, email, password_hash, is_admin, account_kind
            ) VALUES ($1, 'provider-controller', 'controller@example.test', '!service!', true, 'service')""",
            uid,
        )
        await pool.execute(
            """INSERT INTO tokens (
                id, user_id, name, token_hash, token_prefix, key_class, scopes
            ) VALUES ($1, $2, 'provider-control', $3, $4, 'service', ARRAY['read','write','admin'])""",
            tid, uid, token_hash, token_prefix,
        )
        app = FastAPI()
        app.include_router(admin_sso.router, prefix="/api/v1")

        @app.exception_handler(AKBError)
        async def auth_error(_request, exc):
            return JSONResponse({"error": exc.message}, status_code=exc.status_code)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://akb.example") as client:
            headers = {"Authorization": f"Bearer {raw}"}
            catalog = await client.get("/api/v1/admin/sso/providers", headers=headers)
            assert catalog.status_code == 200
            assert catalog.json()["providers"][0]["alias"] == "workforce"
            payload = {
                "existing_user_id": str(uuid.uuid4()),
                "upstream_subject": "exact-upstream-subject",
                "broker_subject": "exact-broker-subject",
            }
            endpoint = f"/api/v1/admin/sso/providers/workforce/identity-migrations/{operation}"
            denied = await client.post(endpoint, headers=headers, json=payload)
            assert denied.status_code == 401
            # A double-submit string does not turn a PAT into an admin session.
            csrf = "unbound-csrf-value-that-is-long-enough"
            client.cookies.set("__Host-akb_admin_csrf", csrf)
            forged = await client.post(
                endpoint, headers={**headers, "X-AKB-Admin-CSRF": csrf}, json=payload,
            )
            assert forged.status_code == 401

        assert await pool.fetchval("SELECT COUNT(*) FROM tokens WHERE id=$1", tid) == 1
        assert await pool.fetchval("SELECT COUNT(*) FROM external_identities") == 0
        assert await pool.fetchval("SELECT COUNT(*) FROM admin_browser_sessions") == 0
