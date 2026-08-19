"""Local parity for the identity provider's forced credential change.

In SSO mode a delivered credential is temporary: the provider arms
``UPDATE_PASSWORD``, so the first successful sign-in has to replace it. These
are the local-mode contracts for the same promise — including the one that
says nothing changes for an account that was never issued a credential.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.exceptions import AKBError, CredentialChangeRequiredError


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_CHOSEN_PASSWORD = "chosen-by-the-holder"  # pragma: allowlist secret
_DELIVERED_PASSWORD = "delivered-out-of-band"  # pragma: allowlist secret
_REPLACEMENT_PASSWORD = "replacement-chosen-now"  # pragma: allowlist secret


def _dsn_for_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(parsed._replace(path=f"/{database}"))


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except OSError, asyncpg.PostgresError:
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    database = f"akb_forced_change_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f'CREATE DATABASE "{database}"')
    pool: asyncpg.Pool | None = None
    try:
        target_dsn = _dsn_for_database(_DSN, database)
        conn = await asyncpg.connect(target_dsn)
        try:
            await conn.execute(_INIT_SQL)
            from app.db.postgres import _load_migration

            events_migration = _load_migration("015_events_outbox.py")
            assert events_migration is not None
            await events_migration.migrate(conn=conn)
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(target_dsn, min_size=1, max_size=8)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


class _RoleSync:
    """PG-native role hooks are best-effort lifecycle noise for these tests."""

    async def on_user_create(self, user_id) -> None:
        return None

    async def on_user_delete(self, user_id) -> None:
        return None

    async def revoke_token_role_strict(self, token_id) -> None:
        return None


@pytest.fixture
async def pool(monkeypatch):
    async with _fresh_database() as pool:
        from app.services import (
            access_service,
            account_service,
            auth_service,
            password_service,
            recovery_admin_service,
        )

        async def _get_pool():
            return pool

        role_sync = _RoleSync()
        for module in (
            access_service,
            account_service,
            auth_service,
            password_service,
            recovery_admin_service,
        ):
            monkeypatch.setattr(module, "get_pool", _get_pool)
        for module in (
            access_service,
            account_service,
            auth_service,
            recovery_admin_service,
        ):
            monkeypatch.setattr(module, "get_role_sync", lambda: role_sync)
        monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
        yield pool


async def _credential_change_required(pool, user_id) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT credential_change_required FROM users WHERE id = $1",
            uuid.UUID(str(user_id)),
        )


async def _signed_in(username: str, password: str) -> str:
    """Sign in and hold until the returned session is usable.

    ``login`` pins ``iat`` past the account's revocation cutoff, so a token
    minted right after a reset carries an ``nbf`` up to a second ahead. That
    is pre-existing revocation behavior; waiting it out here keeps these
    tests about the credential-change marker.
    """
    from app.services.auth_service import login

    session = await login(username, password)
    await asyncio.sleep(1.1)
    return session["token"]


async def _registered(pool) -> dict:
    """An ordinary account whose password its own holder chose."""
    from app.services.auth_service import register

    suffix = uuid.uuid4().hex[:10]
    return await register(
        f"holder-{suffix}",
        f"holder-{suffix}@example.com",
        _CHOSEN_PASSWORD,
    )


# ── Which paths arm the marker ──────────────────────────────────────


async def test_self_service_registration_owes_no_credential_change(pool):
    account = await _registered(pool)
    assert await _credential_change_required(pool, account["user_id"]) is False


async def test_password_reset_leaves_the_account_owing_a_change(pool):
    from app.services.password_service import reset_password

    account = await _registered(pool)
    await reset_password(username=account["username"], actor_id=None, method="cli")
    assert await _credential_change_required(pool, account["user_id"]) is True


async def test_local_recovery_admin_provisioning_arms_the_marker(pool):
    from app.services.recovery_admin_service import provision_local_recovery_admin

    report = await provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_DELIVERED_PASSWORD,
    )
    assert report["created"] is True
    assert await _credential_change_required(pool, report["user_id"]) is True


async def test_repeat_provisioning_does_not_re_arm_a_cleared_requirement(pool):
    from app.services.auth_service import change_password
    from app.services.recovery_admin_service import provision_local_recovery_admin

    kwargs = {
        "username": "recovery-admin",
        "email": "recovery-admin@example.com",
        "password": _DELIVERED_PASSWORD,
    }
    report = await provision_local_recovery_admin(**kwargs)
    await change_password(
        report["user_id"], _DELIVERED_PASSWORD, _REPLACEMENT_PASSWORD
    )

    repeat = await provision_local_recovery_admin(**kwargs)

    assert repeat["created"] is False
    assert await _credential_change_required(pool, report["user_id"]) is False


# ── An account that was never issued a credential is unaffected ─────


async def test_an_account_that_never_had_a_credential_issued_is_unaffected(pool):
    """The regression this whole change must not cause."""
    from app.services.auth_service import resolve_rest_user_authorization

    account = await _registered(pool)
    token = await _signed_in(account["username"], _CHOSEN_PASSWORD)

    resolved = await resolve_rest_user_authorization(f"Bearer {token}")

    assert resolved is not None
    assert resolved.user_id == account["user_id"]
    assert await _credential_change_required(pool, account["user_id"]) is False


async def test_migration_080_leaves_pre_existing_accounts_unaffected(pool):
    """The upgrade path: existing rows must not acquire the requirement."""
    from app.db.postgres import _load_migration
    from app.services.auth_service import hash_password

    migration = _load_migration("080_local_forced_credential_change.py")
    assert migration is not None

    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE users DROP COLUMN credential_change_required"
        )
        for index in range(2):
            suffix = f"{uuid.uuid4().hex[:8]}-{index}"
            await conn.execute(
                "INSERT INTO users (username, email, password_hash)"
                " VALUES ($1, $2, $3)",
                f"pre-{suffix}",
                f"pre-{suffix}@example.com",
                hash_password(_CHOSEN_PASSWORD),
            )
        await migration.migrate(conn)

        assert await conn.fetchval("SELECT COUNT(*) FROM users") == 2
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE credential_change_required"
            )
            == 0
        )


# ── While the marker is set ─────────────────────────────────────────


async def _issued_session(pool) -> tuple[dict, str, str]:
    """An account holding a credential someone else generated for it."""
    from app.services.password_service import reset_password

    account = await _registered(pool)
    delivered, _ = await reset_password(
        username=account["username"], actor_id=None, method="admin_ui"
    )
    return account, delivered, await _signed_in(account["username"], delivered)


async def test_a_delivered_credential_still_signs_in(pool):
    """Sign-in has to work, or there is no session to change the password."""
    _, _, token = await _issued_session(pool)
    assert token


async def test_the_resulting_session_is_refused_off_the_change_route(pool):
    from app.services.auth_service import resolve_rest_user_authorization

    _, _, token = await _issued_session(pool)
    with pytest.raises(CredentialChangeRequiredError):
        await resolve_rest_user_authorization(f"Bearer {token}")


async def test_delegated_human_authorization_is_refused_too(pool):
    """The other boundary a local session becomes authority through."""
    from app.services.auth_service import resolve_delegated_human_authorization

    _, _, token = await _issued_session(pool)
    with pytest.raises(CredentialChangeRequiredError):
        await resolve_delegated_human_authorization(f"Bearer {token}")


async def test_the_credential_change_resolution_is_the_one_exemption(pool):
    from app.services.auth_service import (
        resolve_rest_credential_change_authorization,
    )

    account, _, token = await _issued_session(pool)
    resolved = await resolve_rest_credential_change_authorization(f"Bearer {token}")
    assert resolved is not None
    assert resolved.user_id == account["user_id"]


async def test_a_personal_access_token_is_not_gated_by_the_marker(pool):
    """A PAT is not the credential that was delivered.

    Reset already leaves PATs working on purpose, so pin that boundary here
    rather than letting a later change move it by accident.
    """
    from app.services.auth_service import (
        create_pat,
        resolve_rest_user_authorization,
    )
    from app.services.password_service import reset_password

    account = await _registered(pool)
    credential = await create_pat(account["user_id"], "integration")
    await reset_password(username=account["username"], actor_id=None, method="cli")

    resolved = await resolve_rest_user_authorization(f"Bearer {credential['token']}")

    assert resolved is not None
    assert resolved.user_id == account["user_id"]


# ── Clearing it ─────────────────────────────────────────────────────


async def test_changing_the_password_clears_the_requirement(pool):
    from app.services.auth_service import change_password, resolve_rest_user_authorization
    from app.services.password_service import reset_password

    account = await _registered(pool)
    delivered, _ = await reset_password(
        username=account["username"], actor_id=None, method="admin_ui"
    )

    await change_password(account["user_id"], delivered, _REPLACEMENT_PASSWORD)

    assert await _credential_change_required(pool, account["user_id"]) is False
    token = await _signed_in(account["username"], _REPLACEMENT_PASSWORD)
    resolved = await resolve_rest_user_authorization(f"Bearer {token}")
    assert resolved is not None


# ── The route seam ──────────────────────────────────────────────────


def _app() -> FastAPI:
    """The real auth router, with the application's own error envelope."""
    from app.api.routes import auth as auth_routes

    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api/v1")

    @app.exception_handler(AKBError)
    async def _akb_error(request, exc: AKBError):
        detail: object = exc.message
        if exc.code:
            detail = {"message": exc.message, "code": exc.code}
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    return app


async def test_every_other_route_refuses_and_the_change_route_escapes(pool):
    account, delivered, token = await _issued_session(pool)
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=_app())

    async with httpx.AsyncClient(
        transport=transport, base_url="http://forced-change.test"
    ) as client:
        refused = await client.get("/api/v1/auth/me", headers=headers)
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "credential_change_required"

        changed = await client.post(
            "/api/v1/auth/change-password",
            headers=headers,
            json={
                "current_password": delivered,
                "new_password": _REPLACEMENT_PASSWORD,
            },
        )
        assert changed.status_code == 200

        # Changing the password revokes the session that changed it, so the
        # account signs in again — and then reaches the route it was refused.
        replacement = await _signed_in(account["username"], _REPLACEMENT_PASSWORD)
        allowed = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {replacement}"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["username"] == account["username"]
