"""Stable OIDC identity resolution and managed admission tests."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager, suppress
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
from fastapi import FastAPI, Request

from app.config import settings
from app.exceptions import (
    AccountSuspendedError,
    ConflictError,
    ExternalIdentityConflictError,
    MembershipRequiredError,
)


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_ISSUER = "https://id.example.com/realms/akb"
_LOCK_TEST_TIMEOUT = 10


@pytest.fixture
async def pool(monkeypatch):
    try:
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=5)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    from app.db.postgres import _load_migration
    from app.services import auth_service

    migration = _load_migration("043_workspace_account_governance.py")
    assert migration is not None
    async with pool.acquire() as conn:
        await migration.migrate(conn=conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "open", raising=False)
    monkeypatch.setattr(settings, "keycloak_require_verified_email", True, raising=False)
    yield pool

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username LIKE 'wsg-%'")
        await conn.execute("DELETE FROM events WHERE actor_id LIKE 'wsg-%'")
    await pool.close()


@pytest.fixture
async def bearer_client(pool, monkeypatch):
    from app.api.routes import access, auth
    from app.services import access_service

    async def _get_pool():
        return pool

    monkeypatch.setattr(access_service, "get_pool", _get_pool)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(access.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example") as client:
        yield client


def _claims(subject: str, email: str | None = None) -> dict:
    claims = {
        "iss": _ISSUER,
        "sub": subject,
        "preferred_username": f"wsg-{subject}",
    }
    if email is not None:
        claims.update({"email": email, "email_verified": True})
    return claims


def _stub_verified_bearers(monkeypatch, claims_by_token, *, barrier: asyncio.Barrier | None = None):
    from app.services import auth_service
    from app.services.auth_verifier_profiles import KEYCLOAK_ACCESS_V1, VerifiedPrincipal

    async def verify(token, route_profile):
        assert route_profile in {"api", "mcp"}
        claims = claims_by_token[token]
        if barrier is not None:
            await barrier.wait()
        return VerifiedPrincipal(
            profile_id=KEYCLOAK_ACCESS_V1,
            issuer=claims["iss"],
            subject=claims["sub"],
            credential_type="access_token",
            claims={**claims, "scope": "openid profile email akb:vault:read"},
            audience=f"https://akb.example/{route_profile}",
        )

    monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", verify)
    monkeypatch.setattr(settings, "auth_mode", "sso")


def _bearer_headers(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {subject}"}


async def _insert_bound_user(
    pool,
    *,
    subject: str,
    email: str,
    status: str = "active",
    is_admin: bool = False,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, auth_provider,
                account_status, account_kind, is_admin
            ) VALUES ($1, $2, $3, $4, 'keycloak', $5, 'human', $6)
            """,
            user_id,
            f"wsg-{uuid.uuid4().hex[:12]}",
            email,
            "!keycloak-sso:no-local-login!",
            status,
            is_admin,
        )
        await conn.execute(
            """
            INSERT INTO external_identities (user_id, issuer, subject, email_snapshot)
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            _ISSUER,
            subject,
            email,
        )
    return user_id


async def test_enrollment_mode_defaults_to_open():
    assert type(settings).model_fields["keycloak_enrollment_mode"].default == "open"


async def test_invite_only_rejects_unbound_verified_realm_user(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)
    with pytest.raises(MembershipRequiredError) as exc_info:
        await _resolve_or_provision_keycloak_user(
            _claims("not-invited", "wsg-not-invited@example.com")
        )
    assert exc_info.value.code == "membership_required"


async def test_exact_subject_binding_preserves_user_id_across_email_change(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    old_email = f"wsg-old-{uuid.uuid4().hex[:8]}@example.com"
    new_email = f"wsg-new-{uuid.uuid4().hex[:8]}@example.com"
    user_id = await _insert_bound_user(
        pool,
        subject="email-change",
        email=old_email,
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)

    resolved = await _resolve_or_provision_keycloak_user(
        _claims("email-change", new_email), sync_profile=True,
    )

    assert resolved["user_id"] == user_id
    assert resolved["newly_provisioned"] is False
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.email, e.email_snapshot
              FROM users u
              JOIN external_identities e ON e.user_id = u.id
             WHERE u.id = $1 AND e.issuer = $2 AND e.subject = $3
            """,
            user_id,
            _ISSUER,
            "email-change",
        )
    assert row["email"] == new_email
    assert row["email_snapshot"] == new_email


async def test_exact_binding_does_not_require_email_claim(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    user_id = await _insert_bound_user(
        pool,
        subject="no-email-needed",
        email=f"wsg-no-email-{uuid.uuid4().hex[:8]}@example.com",
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)

    resolved = await _resolve_or_provision_keycloak_user(_claims("no-email-needed"))
    assert resolved["user_id"] == user_id


async def test_bound_identity_sanitizes_upstream_display_name_without_blocking_login(
    pool, monkeypatch
):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    subject = f"long-name-{uuid.uuid4().hex[:8]}"
    user_id = await _insert_bound_user(
        pool,
        subject=subject,
        email=f"wsg-{subject}@example.com",
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)
    claims = _claims(subject)
    claims["name"] = "  Alice\n" + ("A" * 120)

    resolved = await _resolve_or_provision_keycloak_user(claims, sync_profile=True)

    assert resolved["user_id"] == user_id
    assert len(resolved["display_name"]) == 100
    assert "\n" not in resolved["display_name"]


async def test_jit_provisioning_sanitizes_upstream_display_name(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    subject = f"jit-long-name-{uuid.uuid4().hex[:8]}"
    claims = _claims(subject, f"wsg-{subject}@example.com")
    claims["name"] = "  Alice\n" + ("B" * 120)

    resolved = await _resolve_or_provision_keycloak_user(claims)

    assert resolved["newly_provisioned"] is True
    assert len(resolved["display_name"]) == 100
    assert "\n" not in resolved["display_name"]


@pytest.mark.parametrize("auth_provider", ["local", "keycloak"])
async def test_open_mode_never_adopts_existing_user_by_email(
    pool,
    auth_provider,
):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    user_id = uuid.uuid4()
    email = f"wsg-backfill-{uuid.uuid4().hex[:8]}@example.com"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, auth_provider)
            VALUES ($1, $2, $3, $4, $5)
            """,
            user_id,
            f"wsg-{uuid.uuid4().hex[:12]}",
            email,
            "!keycloak-sso:no-local-login!",
            auth_provider,
        )

    with pytest.raises(ExternalIdentityConflictError):
        await _resolve_or_provision_keycloak_user(
            _claims("backfill-existing", email)
        )

    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM external_identities WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            "backfill-existing",
        ) == 0
        assert await conn.fetchval(
            "SELECT auth_provider FROM users WHERE id = $1",
            user_id,
        ) == auth_provider


async def test_concurrent_open_mode_creation_of_same_subject_is_idempotent(
    pool,
    monkeypatch,
):
    from app.services import auth_service

    email = f"wsg-concurrent-jit-{uuid.uuid4().hex[:8]}@example.com"

    original_lookup = auth_service._bound_external_user
    first_lookup_started = asyncio.Event()
    release_first_lookup = asyncio.Event()
    first_pid = None

    async def pause_first_lookup(conn, issuer, subject, *, lock_user=False):
        nonlocal first_pid
        if first_pid is None and not lock_user:
            first_pid = conn.get_server_pid()
            first_lookup_started.set()
            await release_first_lookup.wait()
        return await original_lookup(conn, issuer, subject, lock_user=lock_user)

    monkeypatch.setattr(auth_service, "_bound_external_user", pause_first_lookup)
    claims = _claims("concurrent-jit", email)
    tasks = []
    try:
        async with asyncio.timeout(10):
            tasks.append(asyncio.create_task(
                auth_service._resolve_or_provision_keycloak_user(claims)
            ))
            await first_lookup_started.wait()
            tasks.append(asyncio.create_task(
                auth_service._resolve_or_provision_keycloak_user(claims)
            ))
            # Wait for an actual PostgreSQL lock waiter instead of requiring
            # both callbacks to pass the lock and deadlocking a two-party barrier.
            async with pool.acquire() as observer:
                while not await observer.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity"
                    " WHERE $1::integer = ANY(pg_blocking_pids(pid)))",
                    first_pid,
                ):
                    await asyncio.sleep(0.01)
            release_first_lookup.set()
            results = await asyncio.gather(*tasks)
    finally:
        release_first_lookup.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert len({result["user_id"] for result in results}) == 1
    assert sorted(result["newly_provisioned"] for result in results) == [False, True]
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM external_identities WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            "concurrent-jit",
        ) == 1
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE email = $1",
            email,
        ) == 1


async def test_open_mode_jit_creates_user_and_subject_binding(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"wsg-jit-{uuid.uuid4().hex[:8]}@example.com"
    resolved = await _resolve_or_provision_keycloak_user(_claims("jit-new", email))

    assert resolved["newly_provisioned"] is True
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.account_status, u.account_kind, e.issuer, e.subject
              FROM users u
              JOIN external_identities e ON e.user_id = u.id
             WHERE u.id = $1
            """,
            resolved["user_id"],
        )
    assert row["account_status"] == "active"
    assert row["account_kind"] == "human"
    assert row["issuer"] == _ISSUER
    assert row["subject"] == "jit-new"


async def test_first_keycloak_api_principal_is_never_bootstrap_admin(
    monkeypatch,
):
    """OIDC roles and empty-database order never grant AKB authority."""
    try:
        admin = await asyncpg.connect(_DSN)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    schema = "external_jit_" + uuid.uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    isolated_pool = None
    try:
        isolated_pool = await asyncpg.create_pool(
            _DSN,
            min_size=1,
            max_size=2,
            server_settings={"search_path": schema},
        )
        async with isolated_pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE users (
                    id UUID PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT,
                    is_admin BOOLEAN NOT NULL DEFAULT false,
                    tokens_revoked_before TIMESTAMPTZ NOT NULL DEFAULT
                        TIMESTAMPTZ '1970-01-01 00:00:00+00',
                    auth_provider TEXT NOT NULL DEFAULT 'local',
                    account_status TEXT NOT NULL DEFAULT 'active',
                    account_kind TEXT NOT NULL DEFAULT 'human',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE external_identities (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    email_snapshot TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (issuer, subject)
                );
                CREATE TABLE pending_admissions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    email TEXT,
                    display_name TEXT,
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    arrivals INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (issuer, subject)
                );
                CREATE TABLE events (
                    id BIGSERIAL PRIMARY KEY,
                    vault_id UUID,
                    kind TEXT NOT NULL,
                    resource_uri TEXT,
                    actor_id TEXT,
                    payload JSONB NOT NULL
                );
                """
            )

        from app.services import auth_service
        from app.services.auth_verifier_profiles import (
            KEYCLOAK_ACCESS_V1,
            VerifiedPrincipal,
        )

        async def _get_pool():
            return isolated_pool

        class RoleSync:
            async def on_user_create(self, _user_id):
                return None

        claims = {
            **_claims("first-api-user", "wsg-first-api-user@example.com"),
            "scope": "openid profile email akb:vault:read",
            "realm_access": {"roles": ["admin", "realm-admin"]},
            "resource_access": {"akb-web": {"roles": ["admin"]}},
        }
        principal = VerifiedPrincipal(
            profile_id=KEYCLOAK_ACCESS_V1,
            issuer=_ISSUER,
            subject="first-api-user",
            credential_type="access_token",
            claims=claims,
            audience="https://akb.example.com/api",
        )

        async def _verify_api(_token, route_profile):
            assert route_profile == "api"
            return principal

        monkeypatch.setattr(auth_service, "get_pool", _get_pool)
        monkeypatch.setattr(auth_service, "get_role_sync", RoleSync)
        monkeypatch.setattr(auth_service, "verify_keycloak_access_v1", _verify_api)
        monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
        monkeypatch.setattr(settings, "keycloak_enrollment_mode", "open", raising=False)
        monkeypatch.setattr(settings, "keycloak_require_verified_email", True, raising=False)

        actor = await auth_service.resolve_rest_user_authorization(
            "Bearer verified-keycloak-api-token"
        )

        assert actor is not None
        assert actor.auth_method == "oauth"
        assert actor.is_admin is False
        async with isolated_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT is_admin FROM users WHERE username = $1",
                "wsg-first-api-user",
            )
        assert row["is_admin"] is False
    finally:
        if isolated_pool is not None:
            await isolated_pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_second_subject_cannot_jit_bind_to_an_already_bound_email(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"wsg-collision-{uuid.uuid4().hex[:8]}@example.com"
    await _insert_bound_user(pool, subject="subject-one", email=email)

    with pytest.raises(ExternalIdentityConflictError):
        await _resolve_or_provision_keycloak_user(_claims("subject-two", email))


async def test_open_mode_allocates_distinct_handle_on_username_collision(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    username = f"wsg-username-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (username, email, password_hash, auth_provider)
            VALUES ($1, $2, $3, 'local')
            """,
            username,
            f"{username}-existing@example.com",
            "!existing!",
        )

    claims = _claims("username-collision", f"{username}-new@example.com")
    claims["preferred_username"] = username
    resolved = await _resolve_or_provision_keycloak_user(claims)

    assert resolved["newly_provisioned"] is True
    assert resolved["username"] != username
    assert resolved["username"].startswith(f"{username}-")
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE username IN ($1, $2)",
            username,
            resolved["username"],
        ) == 2


@pytest.mark.parametrize("legacy_stem", ["Straße", "ΑΟΣ"])
async def test_open_mode_detects_legacy_unicode_handle_with_database_lowercase(
    pool, legacy_stem
):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    existing_id = uuid.uuid4()
    legacy_username = f"wsg-{legacy_stem}-{uuid.uuid4().hex[:6]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, auth_provider,
                account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'local', 'active', 'human')
            """,
            existing_id,
            legacy_username,
            f"{uuid.uuid4().hex}@example.com",
            "unused",
        )
        database_lower = await conn.fetchval("SELECT lower($1::text)", legacy_username)

    claims = _claims(
        f"unicode-collision-{uuid.uuid4().hex[:8]}",
        f"wsg-unicode-{uuid.uuid4().hex[:8]}@example.com",
    )
    claims["preferred_username"] = legacy_username
    resolved = await _resolve_or_provision_keycloak_user(claims)

    assert resolved["newly_provisioned"] is True
    assert resolved["user_id"] != existing_id
    assert resolved["username"] != database_lower
    assert resolved["username"].startswith(f"{database_lower}-")


async def test_local_registration_rejects_exact_legacy_greek_handle(
    pool, monkeypatch
):
    from app.services import auth_service

    legacy_username = f"wsg-ΑΟΣ-{uuid.uuid4().hex[:6]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, auth_provider,
                account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'local', 'active', 'human')
            """,
            uuid.uuid4(),
            legacy_username,
            f"{uuid.uuid4().hex}@example.com",
            "unused",
        )

    monkeypatch.setattr(auth_service, "require_local_auth_enabled", lambda: None)
    monkeypatch.setattr(
        auth_service, "hash_password_async", AsyncMock(return_value="unused")
    )
    with pytest.raises(ConflictError):
        await auth_service.register(
            legacy_username,
            f"wsg-register-{uuid.uuid4().hex}@example.com",
            "password123",
        )


async def test_login_prefers_exact_database_canonical_handle_among_legacy_variants(
    pool, monkeypatch
):
    from app.services import auth_service

    suffix = uuid.uuid4().hex[:6]
    mixed_username = f"wsg-Alice-{suffix}"
    canonical_username = f"wsg-alice-{suffix}"
    mixed_id = uuid.uuid4()
    canonical_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO users (
                id, username, email, password_hash, auth_provider,
                account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'local', 'active', 'human')
            """,
            [
                (
                    mixed_id,
                    mixed_username,
                    f"{uuid.uuid4().hex}@example.com",
                    "mixed-hash",
                ),
                (
                    canonical_id,
                    canonical_username,
                    f"{uuid.uuid4().hex}@example.com",
                    "canonical-hash",
                ),
            ],
        )

    monkeypatch.setattr(auth_service, "require_local_auth_enabled", lambda: None)
    def matches_fixture_hash(_password: str, password_hash: str) -> bool:
        return password_hash == "canonical-hash"  # pragma: allowlist secret

    monkeypatch.setattr(
        auth_service,
        "verify_password_async",
        AsyncMock(side_effect=matches_fixture_hash),
    )
    monkeypatch.setattr(auth_service, "create_jwt", lambda *_args, **_kwargs: "token")

    result = await auth_service.login(canonical_username.upper(), "password123")

    assert result["user"]["id"] == str(canonical_id)


async def test_suspended_bound_user_is_denied(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    await _insert_bound_user(
        pool,
        subject="suspended",
        email=f"wsg-suspended-{uuid.uuid4().hex[:8]}@example.com",
        status="suspended",
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)

    with pytest.raises(AccountSuspendedError) as exc_info:
        await _resolve_or_provision_keycloak_user(_claims("suspended"))
    assert exc_info.value.code == "account_suspended"


async def test_disabled_mode_rejects_even_a_bound_external_identity(pool, monkeypatch):
    from app.exceptions import ExternalAuthDisabledError
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    await _insert_bound_user(
        pool,
        subject="disabled-mode",
        email=f"wsg-disabled-{uuid.uuid4().hex[:8]}@example.com",
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "disabled", raising=False)

    with pytest.raises(ExternalAuthDisabledError):
        await _resolve_or_provision_keycloak_user(_claims("disabled-mode"))


async def _profile_rows(pool, *user_ids):
    return await pool.fetch(
        """
        SELECT u.id, u.email, u.display_name, u.updated_at,
               u.xmin::text AS user_version, u.ctid::text AS user_tuple,
               e.email_snapshot, e.last_seen_at,
               e.xmin::text AS binding_version, e.ctid::text AS binding_tuple
          FROM users u
          JOIN external_identities e ON e.user_id = u.id
         WHERE u.id = ANY($1::uuid[])
         ORDER BY u.id
        """,
        user_ids,
    )


async def test_bound_bearers_share_user_lock_without_advisory_locks_or_profile_writes(
    pool, bearer_client, monkeypatch,
):
    from app.api.deps import require_delegated_actor
    from app.services.auth_service import AuthenticatedUser, _resolve_or_provision_keycloak_user
    from app.services.role_authority import role_authority_transaction
    from mcp_server.http_app import MCPApp

    suffix = uuid.uuid4().hex[:8]
    first, second = f"parallel-first-{suffix}", f"parallel-second-{suffix}"
    first_email, second_email = f"wsg-{first}@example.com", f"wsg-{second}@example.com"
    first_id = await _insert_bound_user(pool, subject=first, email=first_email)
    second_id = await _insert_bound_user(pool, subject=second, email=second_email)
    claims = {
        first: {**_claims(first, second_email), "name": "Changed first name"},
        second: {**_claims(second, f"wsg-{second}-changed@example.com"), "name": "Changed second name"},
    }
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only")
    _stub_verified_bearers(monkeypatch, claims, barrier=asyncio.Barrier(3))
    before = await _profile_rows(pool, first_id, second_id)

    service_user = AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="fixture-service",
        email="fixture-service@example.com",
        display_name=None,
        is_admin=False,
        auth_method="pat",
        account_kind="service",
        token_id=str(uuid.uuid4()),
        key_class="service",
    )
    delegated_request = Request({
        "type": "http",
        "headers": [(b"x-akb-delegated-authorization", f"Bearer {second}".encode())],
    })
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=MCPApp()), base_url="https://akb.example",
    ) as mcp_client:
        async with pool.acquire() as holder, role_authority_transaction(holder):
            for subject in claims:
                await holder.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"external-identity:{len(_ISSUER)}:{_ISSUER}{subject}",
                )
            # Same-user authentication must be compatible with a shared user lock.
            await holder.fetchval("SELECT id FROM users WHERE id = $1 FOR SHARE", first_id)
            async with asyncio.timeout(_LOCK_TEST_TIMEOUT), asyncio.TaskGroup() as tasks:
                rest = tasks.create_task(bearer_client.get("/api/v1/auth/me", headers=_bearer_headers(first)))
                mcp = tasks.create_task(mcp_client.delete("/mcp", headers=_bearer_headers(first)))
                delegated = tasks.create_task(require_delegated_actor(delegated_request, service_user))

    assert rest.result().status_code == 200
    assert rest.result().json()["user_id"] == str(first_id)
    assert rest.result().json()["email"] == first_email
    assert mcp.result().status_code == 200
    assert mcp.result().json() == {"terminated": True}
    assert delegated.result().user.user_id == str(second_id)
    assert delegated.result().user.email == second_email
    # Compare tuple versions as well as values so even a no-op UPDATE fails.
    assert await _profile_rows(pool, first_id, second_id) == before

    with pytest.raises(ExternalIdentityConflictError):
        await _resolve_or_provision_keycloak_user(claims[first], sync_profile=True)
    assert await _profile_rows(pool, first_id, second_id) == before


async def _wait_for_user_row_blocker(observer, writer_pid):
    while True:
        wait_event = await observer.fetchval(
            """
            SELECT wait_event FROM pg_stat_activity
             WHERE datname = current_database()
               AND wait_event_type = 'Lock'
               AND $1::int = ANY(pg_blocking_pids(pid))
            """,
            writer_pid,
        )
        if wait_event is not None:
            assert wait_event in {"transactionid", "tuple"}
            return
        await asyncio.sleep(0.01)


class _RollBackGovernanceChange(Exception):
    pass


@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
@pytest.mark.parametrize(("mutation", "committed_status"), [("suspend", 401), ("demote", 403)])
async def test_bound_bearer_waits_for_same_user_governance_change(
    pool, bearer_client, monkeypatch, commit, mutation, committed_status,
):
    from app.services import account_service
    from app.services.role_authority import role_authority_transaction

    suffix = uuid.uuid4().hex[:8]
    subject, other = f"governance-target-{suffix}", f"governance-other-{suffix}"
    user_id = await _insert_bound_user(pool, subject=subject, email=f"wsg-{subject}@example.com", is_admin=True)
    other_id = await _insert_bound_user(pool, subject=other, email=f"wsg-{other}@example.com")
    _stub_verified_bearers(monkeypatch, {subject: _claims(subject), other: _claims(other)})

    writer_pid = asyncio.get_running_loop().create_future()
    release_writer = asyncio.Event()

    async def _get_pool():
        return pool

    @asynccontextmanager
    async def pause_before_governance_commit(conn):
        async with role_authority_transaction(conn):
            yield
            if not writer_pid.done():
                writer_pid.set_result(conn.get_server_pid())
                await release_writer.wait()
                if not commit:
                    raise _RollBackGovernanceChange

    monkeypatch.setattr(account_service, "get_pool", _get_pool)
    monkeypatch.setattr(account_service, "role_authority_transaction", pause_before_governance_commit)

    async def change_account():
        actor_id = f"wsg-governance-admin-{suffix}"
        with suppress(_RollBackGovernanceChange):
            if mutation == "suspend":
                await account_service.suspend_user(str(user_id), actor_id=actor_id)
            else:
                await account_service.set_user_admin(str(user_id), is_admin=False, actor_id=actor_id)

    async with asyncio.timeout(_LOCK_TEST_TIMEOUT), asyncio.TaskGroup() as tasks:
        tasks.create_task(change_account())
        pid = await writer_pid
        headers = _bearer_headers(subject)
        if mutation == "demote" and commit:
            request = bearer_client.put(
                f"/api/v1/admin/users/{user_id}/role", headers=headers, json={"is_admin": True},
            )
        else:
            request = bearer_client.get("/api/v1/admin/users", headers=headers)
        pending = tasks.create_task(request)
        async with pool.acquire() as observer:
            await _wait_for_user_row_blocker(observer, pid)
        unrelated = await bearer_client.get("/api/v1/auth/me", headers=_bearer_headers(other))
        assert unrelated.status_code == 200
        assert unrelated.json()["user_id"] == str(other_id)
        assert not pending.done()
        release_writer.set()

    response = pending.result()
    assert response.status_code == (committed_status if commit else 200)
    if response.status_code == 200:
        assert any(user["id"] == str(user_id) and user["is_admin"] for user in response.json()["users"])
    elif response.status_code == 403:
        assert response.json() == {"detail": "Admin only"}
        profile = await bearer_client.get("/api/v1/auth/me", headers=_bearer_headers(subject))
        assert profile.status_code == 200
        assert profile.json()["is_admin"] is False


async def test_bound_bearer_checks_binding_again_after_waiting_for_user_row(pool, bearer_client, monkeypatch):
    from app.services.role_authority import role_authority_transaction

    subject = f"deleted-binding-{uuid.uuid4().hex[:8]}"
    user_id = await _insert_bound_user(pool, subject=subject, email=f"wsg-{subject}@example.com")
    _stub_verified_bearers(monkeypatch, {subject: _claims(subject)})

    async with asyncio.timeout(_LOCK_TEST_TIMEOUT), asyncio.TaskGroup() as tasks:
        async with pool.acquire() as writer, pool.acquire() as observer:
            async with role_authority_transaction(writer):
                await writer.fetchval("SELECT id FROM users WHERE id = $1 FOR NO KEY UPDATE", user_id)
                await writer.execute("DELETE FROM external_identities WHERE user_id = $1", user_id)
                pending = tasks.create_task(bearer_client.get("/api/v1/auth/me", headers=_bearer_headers(subject)))
                await _wait_for_user_row_blocker(observer, writer.get_server_pid())

    assert pending.result().status_code == 401
    assert await pool.fetchval("SELECT account_status FROM users WHERE id = $1", user_id) == "active"
    assert await pool.fetchval("SELECT id FROM external_identities WHERE user_id = $1", user_id) is None


async def test_external_identity_profile_snapshot_is_not_locally_editable(pool):
    from app.exceptions import ExternalProfileReadOnlyError
    from app.services.auth_service import update_profile

    user_id = await _insert_bound_user(
        pool,
        subject="profile-read-only",
        email=f"wsg-profile-{uuid.uuid4().hex[:8]}@example.com",
    )

    with pytest.raises(ExternalProfileReadOnlyError):
        await update_profile(str(user_id), email="self-asserted@example.com")
    with pytest.raises(ExternalProfileReadOnlyError):
        await update_profile(str(user_id), display_name="Self asserted")
