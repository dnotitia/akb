import asyncpg
import httpx
import pytest

from tests.rest_integration.helpers import register_user
from tests.rest_integration.security_helpers import bearer, make_admin

pytestmark = pytest.mark.live_services

ME = "/api/v1/auth/me"
REVOKE_ALL = "/api/v1/auth/revoke-all-sessions"
PASSWORD = "test1234"  # pragma: allowlist secret


async def login(api: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await api.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return bearer(response.json()["token"])


async def test_revoking_all_sessions_invalidates_jwt_but_keeps_pat_and_allows_fresh_login(
    api: httpx.AsyncClient,
) -> None:
    user = await register_user(api, label="jwt-self-revoke", password=PASSWORD)
    jwt_headers = bearer(user.jwt)
    assert (await api.get(ME, headers=jwt_headers)).status_code == 200

    revoked = await api.post(REVOKE_ALL, headers=jwt_headers)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_before"]
    assert (await api.get(ME, headers=jwt_headers)).status_code == 401
    assert (await api.get(ME, headers=user.headers)).status_code == 200

    fresh = await login(api, user.username, PASSWORD)
    assert (await api.get(ME, headers=fresh)).status_code == 200


async def test_changing_password_invalidates_previous_jwt_and_allows_new_login(api: httpx.AsyncClient) -> None:
    user = await register_user(api, label="jwt-password-change", password=PASSWORD)
    jwt_headers = bearer(user.jwt)
    assert (await api.get(ME, headers=jwt_headers)).status_code == 200

    changed = await api.post(
        "/api/v1/auth/change-password",
        headers=jwt_headers,
        json={"current_password": PASSWORD, "new_password": "new12345"},  # pragma: allowlist secret
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["ok"] is True
    assert (await api.get(ME, headers=jwt_headers)).status_code == 401

    fresh = await login(api, user.username, "new12345")
    assert (await api.get(ME, headers=fresh)).status_code == 200


async def test_only_admin_can_force_logout_another_account(api: httpx.AsyncClient, pg_pool: asyncpg.Pool) -> None:
    admin = await register_user(api, label="jwt-force-admin", password=PASSWORD)
    target = await register_user(api, label="jwt-force-target", password=PASSWORD)
    other = await register_user(api, label="jwt-force-other", password=PASSWORD)
    await make_admin(pg_pool, admin.id)
    admin_headers = await login(api, admin.username, PASSWORD)
    target_headers = bearer(target.jwt)
    force_logout = f"/api/v1/admin/users/{target.id}/revoke-sessions"

    denied = await api.post(force_logout, headers=bearer(other.jwt))
    assert denied.status_code == 403, denied.text
    assert (await api.get(ME, headers=target_headers)).status_code == 200

    revoked = await api.post(force_logout, headers=admin_headers)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_before"]
    assert (await api.get(ME, headers=target_headers)).status_code == 401


async def test_admin_password_reset_invalidates_target_jwt(api: httpx.AsyncClient, pg_pool: asyncpg.Pool) -> None:
    admin = await register_user(api, label="jwt-reset-admin", password=PASSWORD)
    target = await register_user(api, label="jwt-reset-target", password=PASSWORD)
    await make_admin(pg_pool, admin.id)
    admin_headers = await login(api, admin.username, PASSWORD)
    target_headers = bearer(target.jwt)
    assert (await api.get(ME, headers=target_headers)).status_code == 200

    reset = await api.post(f"/api/v1/admin/users/{target.id}/reset-password", headers=admin_headers)
    assert reset.status_code == 200, reset.text
    assert reset.json()["temporary_password"]
    assert (await api.get(ME, headers=target_headers)).status_code == 401
