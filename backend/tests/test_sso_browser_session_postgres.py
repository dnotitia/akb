"""Live-PostgreSQL proof for encrypted ordinary SSO browser sessions."""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_SSO_SESSION_EPOCH = uuid.UUID("8db8b1f8-5f70-43ec-b9c6-039effe26b38")


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


async def _schema_shape(conn) -> dict[str, tuple[tuple[object, ...], ...]]:
    shape: dict[str, tuple[tuple[object, ...], ...]] = {}
    for table in ("sso_browser_sessions", "sso_browser_logout_fences"):
        columns = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = $1
             ORDER BY column_name
            """,
            table,
        )
        constraints = await conn.fetch(
            """
            SELECT conname, contype, pg_get_constraintdef(oid), convalidated
              FROM pg_constraint
             WHERE conrelid = to_regclass($1)
             ORDER BY conname
            """,
            table,
        )
        indexes = await conn.fetch(
            """
            SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid), indisvalid
              FROM pg_index
             WHERE indrelid = to_regclass($1)
             ORDER BY indexrelid::regclass::text
            """,
            table,
        )
        shape[f"{table}.columns"] = tuple(tuple(row) for row in columns)
        shape[f"{table}.constraints"] = tuple(tuple(row) for row in constraints)
        shape[f"{table}.indexes"] = tuple(tuple(row) for row in indexes)
    return shape


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    database = f"akb_sso_browser_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f'CREATE DATABASE "{database}"')
    pool: asyncpg.Pool | None = None
    try:
        target_dsn = _dsn_for_database(_DSN, database)
        conn = await asyncpg.connect(target_dsn)
        try:
            await conn.execute(_INIT_SQL)
            from app.db.postgres import _load_migration

            epoch_migration = _load_migration("076_sso_session_epoch.py")
            assert epoch_migration is not None
            await epoch_migration.migrate(conn=conn)
            fresh_shape = await _schema_shape(conn)
            await conn.execute("DROP TABLE sso_browser_sessions, sso_browser_logout_fences")
            migration = _load_migration("074_sso_browser_sessions.py")
            assert migration is not None
            await migration.migrate(conn=conn)
            await migration.migrate(conn=conn)
            await epoch_migration.migrate(conn=conn)
            await epoch_migration.migrate(conn=conn)
            assert await _schema_shape(conn) == fresh_shape

            events_migration = _load_migration("015_events_outbox.py")
            assert events_migration is not None
            await events_migration.migrate(conn=conn)
            await conn.execute(
                """
                INSERT INTO auth_runtime_state (
                    singleton, runtime_generation, auth_mode,
                    sso_session_epoch
                ) VALUES (TRUE, 1, 'sso', $1)
                """,
                _SSO_SESSION_EPOCH,
            )
            await conn.execute(
                """
                UPDATE auth_runtime_epoch_upgrade
                   SET state = 'enforced'
                 WHERE singleton = TRUE
                """
            )
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(target_dsn, min_size=1, max_size=8)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


def _encoded_key(byte: int = 23) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


def _principal(
    *,
    expiry_seconds: int = 300,
    scope: str = "openid profile email",
    issued_at: int | None = None,
):
    from app.services.auth_verifier_profiles import VerifiedPrincipal

    now = issued_at if issued_at is not None else int(datetime.now(timezone.utc).timestamp())
    claims = {
        "iss": "https://id.example/realms/akb",
        "sub": "subject-1",
        "sid": "keycloak-session-1",
        "identity_provider": "workforce",
        "scope": scope,
        "iat": now,
        "exp": now + expiry_seconds,
    }
    return VerifiedPrincipal(
        profile_id="keycloak-access-v1",
        issuer=claims["iss"],
        subject=claims["sub"],
        credential_type="access_token",
        claims=claims,
        audience="https://akb.example/api",
    )


def _actor(user_id: uuid.UUID):
    from app.services.auth_service import AuthenticatedUser

    return AuthenticatedUser(
        user_id=str(user_id),
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        is_admin=False,
        auth_method="oauth",
        oauth_scopes=["openid", "profile", "email"],
    )


def _tokens(
    *,
    access: str = "access-token-one",
    refresh: str = "refresh-token-one",
    id_token: str = "id-token-one",
    refresh_expires_in: int = 3600,
) -> dict[str, object]:
    return {
        "token_type": "Bearer",
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "refresh_expires_in": refresh_expires_in,
    }


async def _seed_identity(pool) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, display_name,
                auth_provider, account_status, account_kind
            ) VALUES (
                $1, 'alice', 'alice@example.com', '!sso!', 'Alice',
                'keycloak', 'active', 'human'
            )
            """,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO external_identities (id, user_id, issuer, subject)
            VALUES ($1, $2, 'https://id.example/realms/akb', 'subject-1')
            """,
            identity_id,
            user_id,
        )
    return user_id, identity_id


def _configure(monkeypatch, service, pool) -> None:
    from app.config import settings

    async def get_test_pool():
        return pool

    monkeypatch.setattr(service, "get_pool", get_test_pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "auth_runtime_generation", 1, raising=False)
    monkeypatch.setattr(settings, "sso_session_epoch", _SSO_SESSION_EPOCH, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://id.example", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web", raising=False)
    monkeypatch.setattr(settings, "api_oauth_audience", "https://akb.example/api", raising=False)
    monkeypatch.setattr(
        settings,
        "sso_browser_session_encryption_key",
        _encoded_key(),
        raising=False,
    )
    monkeypatch.setattr(settings, "sso_browser_session_idle_ttl_secs", 600, raising=False)
    monkeypatch.setattr(settings, "sso_browser_session_absolute_ttl_secs", 1800, raising=False)
    monkeypatch.setattr(settings, "sso_browser_session_refresh_skew_secs", 30, raising=False)


async def test_opaque_session_is_exact_bound_csrf_safe_and_locally_revocable(
    monkeypatch,
) -> None:
    from app.exceptions import AuthenticationError, ForbiddenError
    from app.services import sso_browser_session_service as service

    async with _fresh_database() as pool:
        _configure(monkeypatch, service, pool)
        user_id, identity_id = await _seed_identity(pool)
        principal = _principal()
        issued = await service.create_sso_browser_session(
            _actor(user_id),
            principal,
            {
                "iss": principal.issuer,
                "sub": principal.subject,
                "sid": principal.claims["sid"],
                "identity_provider": "workforce",
            },
            _tokens(),
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sso_browser_sessions WHERE user_id = $1",
                user_id,
            )
        assert row is not None
        persisted = " ".join(str(value) for value in row.values())
        for secret in (
            issued.token,
            issued.csrf_token,
            "access-token-one",
            "refresh-token-one",
            "id-token-one",
        ):
            assert secret not in persisted
        assert row["external_identity_id"] == identity_id
        assert len(row["token_hash"]) == len(row["csrf_token_hash"]) == 64

        resolved = await service.resolve_sso_browser_session(issued.token)
        assert resolved.user_id == str(user_id)
        assert resolved.auth_method == "browser_session"

        with pytest.raises(ForbiddenError, match="CSRF"):
            await service.resolve_sso_browser_session(
                issued.token,
                require_csrf=True,
                csrf_cookie=issued.csrf_token,
                csrf_header="wrong-csrf-token-that-is-long-enough",
            )
        # CSRF rejection must not become a cross-site logout primitive.
        assert (await service.resolve_sso_browser_session(issued.token)).user_id == str(user_id)

        mutation = await service.resolve_sso_browser_session(
            issued.token,
            require_csrf=True,
            csrf_cookie=issued.csrf_token,
            csrf_header=issued.csrf_token,
        )
        assert mutation.user_id == str(user_id)

        revoked = await service.revoke_sso_browser_session(
            issued.token,
            issued.csrf_token,
            issued.csrf_token,
        )
        assert revoked.refresh_token == "refresh-token-one"
        with pytest.raises(AuthenticationError):
            await service.resolve_sso_browser_session(issued.token)


async def test_rotation_is_serialized_and_persists_only_new_ciphertext(monkeypatch) -> None:
    from app.services import sso_browser_session_service as service

    async with _fresh_database() as pool:
        _configure(monkeypatch, service, pool)
        user_id, _ = await _seed_identity(pool)
        initial = _principal(expiry_seconds=5)
        issued = await service.create_sso_browser_session(
            _actor(user_id),
            initial,
            {
                "iss": initial.issuer,
                "sub": initial.subject,
                "sid": initial.claims["sid"],
                "identity_provider": "workforce",
            },
            _tokens(),
        )

        refreshed = _principal(expiry_seconds=300, scope="openid profile")

        class FakeOIDC:
            refresh_calls = 0

            async def refresh_browser_tokens(self, refresh_token):
                assert refresh_token == "refresh-token-one"
                self.refresh_calls += 1
                await asyncio.sleep(0.05)
                return _tokens(
                    access="access-token-two",
                    refresh="refresh-token-two",
                    id_token="id-token-two",
                )

            async def verify_access_token(self, token, audience, *, route_profile):
                assert (token, audience, route_profile) == (
                    "access-token-two",
                    "https://akb.example/api",
                    "api",
                )
                return refreshed

            async def verify_id_token(self, token, *, client_id):
                assert (token, client_id) == ("id-token-two", "akb-web")
                return {
                    "iss": refreshed.issuer,
                    "sub": refreshed.subject,
                    "sid": refreshed.claims["sid"],
                    "azp": "akb-web",
                    "identity_provider": "workforce",
                }

        oidc = FakeOIDC()
        monkeypatch.setattr(service, "get_keycloak_oidc", lambda: oidc)

        first, second = await asyncio.gather(
            service.resolve_sso_browser_session(issued.token),
            service.resolve_sso_browser_session(issued.token),
        )

        assert first.oauth_scopes == second.oauth_scopes == ["openid", "profile"]
        assert oidc.refresh_calls == 1
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, user_id, token_envelope FROM sso_browser_sessions")
        assert row is not None
        persisted = row["token_envelope"]
        assert "access-token-two" not in persisted
        assert "refresh-token-two" not in persisted
        custody = service._cipher().open(  # noqa: SLF001
            persisted,
            context=service._session_context(row["id"], row["user_id"]),  # noqa: SLF001
        )
        assert custody == {
            "refresh_token": "refresh-token-two",
            "id_token": "id-token-two",
            "scope": "openid profile",
            "provider_alias": "workforce",
        }


async def test_refresh_denial_deletes_but_upstream_outage_preserves_session(
    monkeypatch,
) -> None:
    from app.exceptions import AKBError, AuthenticationError
    from app.services import sso_browser_session_service as service

    async with _fresh_database() as pool:
        _configure(monkeypatch, service, pool)
        user_id, _ = await _seed_identity(pool)

        async def issue():
            principal = _principal(expiry_seconds=5)
            return await service.create_sso_browser_session(
                _actor(user_id),
                principal,
                {
                    "iss": principal.issuer,
                    "sub": principal.subject,
                    "sid": principal.claims["sid"],
                    "identity_provider": "workforce",
                },
                _tokens(),
            )

        class RejectedOIDC:
            async def refresh_browser_tokens(self, _refresh_token):
                raise AuthenticationError("refresh rejected")

        rejected = await issue()
        monkeypatch.setattr(service, "get_keycloak_oidc", lambda: RejectedOIDC())
        with pytest.raises(AuthenticationError):
            await service.resolve_sso_browser_session(rejected.token)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_sessions") == 0

        class UnreachableOIDC:
            async def refresh_browser_tokens(self, _refresh_token):
                raise AKBError("Keycloak unavailable", status_code=502)

        unreachable = await issue()
        monkeypatch.setattr(service, "get_keycloak_oidc", lambda: UnreachableOIDC())
        with pytest.raises(AKBError):
            await service.resolve_sso_browser_session(unreachable.token)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_sessions") == 1

        class VerificationUnavailableOIDC:
            async def refresh_browser_tokens(self, _refresh_token):
                return _tokens(
                    access="access-token-after-refresh",
                    refresh="refresh-token-after-jwks-outage",
                )

            async def verify_access_token(self, _token, _audience, *, route_profile):
                assert route_profile == "api"
                raise AKBError("Keycloak JWKS unavailable", status_code=502)

        monkeypatch.setattr(
            service,
            "get_keycloak_oidc",
            lambda: VerificationUnavailableOIDC(),
        )
        with pytest.raises(AKBError):
            await service.resolve_sso_browser_session(unreachable.token)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, user_id, token_envelope FROM sso_browser_sessions")
        assert row is not None
        custody = service._cipher().open(  # noqa: SLF001
            row["token_envelope"],
            context=service._session_context(row["id"], row["user_id"]),  # noqa: SLF001
        )
        assert custody["refresh_token"] == "refresh-token-after-jwks-outage"


async def test_identity_state_and_verified_backchannel_selector_revoke_immediately(
    monkeypatch,
) -> None:
    from app.exceptions import AuthenticationError
    from app.services import sso_browser_session_service as service

    async with _fresh_database() as pool:
        _configure(monkeypatch, service, pool)
        user_id, identity_id = await _seed_identity(pool)
        principal = _principal()

        async def issue(value=principal):
            return await service.create_sso_browser_session(
                _actor(user_id),
                value,
                {
                    "iss": value.issuer,
                    "sub": value.subject,
                    "sid": value.claims["sid"],
                    "identity_provider": "workforce",
                },
                _tokens(),
            )

        changed = await issue()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE external_identities SET subject = 'replacement' WHERE id = $1",
                identity_id,
            )
        with pytest.raises(AuthenticationError):
            await service.resolve_sso_browser_session(changed.token)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_sessions") == 0
            await conn.execute(
                "UPDATE external_identities SET subject = 'subject-1' WHERE id = $1",
                identity_id,
            )

        suspended = await issue()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET account_status = 'suspended' WHERE id = $1",
                user_id,
            )
        with pytest.raises(AuthenticationError):
            await service.resolve_sso_browser_session(suspended.token)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET account_status = 'active' WHERE id = $1",
                user_id,
            )

        # Administrative suspension deletes the handle transactionally. A
        # quick reactivation cannot revive a cookie that was not presented
        # while the account was suspended.
        from app.services import account_service

        async def get_test_pool():
            return pool

        monkeypatch.setattr(account_service, "get_pool", get_test_pool)
        stale_after_reactivation = await issue()
        await account_service.suspend_user(str(user_id), actor_id="admin-test")
        await account_service.activate_user(str(user_id), actor_id="admin-test")
        with pytest.raises(AuthenticationError):
            await service.resolve_sso_browser_session(stale_after_reactivation.token)

        await issue()
        await issue()
        count = await service.revoke_sso_browser_sessions_from_logout_token(
            issuer=principal.issuer,
            sid="keycloak-session-1",
            subject="subject-1",
            issued_at=principal.claims["iat"],
            expires_at=principal.claims["exp"],
        )
        assert count == 2
        # Back-channel logout is idempotent.
        assert (
            await service.revoke_sso_browser_sessions_from_logout_token(
                issuer=principal.issuer,
                sid="keycloak-session-1",
                subject="subject-1",
                issued_at=principal.claims["iat"],
                expires_at=principal.claims["exp"],
            )
            == 0
        )

        # A callback that resumes after logout is rejected by the durable
        # fence even though no browser-session row existed when logout began.
        with pytest.raises(AuthenticationError, match="logged out"):
            await issue()

        # A genuinely newer login may reuse the same Keycloak sid only when
        # its signed token was issued after the logout event.
        newer = _principal(issued_at=principal.claims["iat"] + 1)
        replacement = await issue(newer)
        assert (await service.resolve_sso_browser_session(replacement.token)).user_id == str(user_id)


async def test_runtime_epoch_reconcile_prevents_mode_transition_resurrection(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.exceptions import AuthenticationError
    from app.services import sso_browser_session_service as service
    from app.services import sso_session_epoch as epoch_service

    async with _fresh_database() as pool:
        _configure(monkeypatch, service, pool)

        async def get_test_pool():
            return pool

        monkeypatch.setattr(epoch_service, "get_pool", get_test_pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM auth_runtime_state")
        first = await epoch_service.reconcile_sso_session_epoch()
        assert first.changed is True
        assert first.auth_mode_changed is False
        assert first.epoch_changed is False
        assert (
            first.ordinary_sessions_revoked,
            first.admin_sessions_revoked,
            first.logout_fences_revoked,
        ) == (0, 0, 0)

        user_id, identity_id = await _seed_identity(pool)
        principal = _principal()

        async def issue_ordinary():
            return await service.create_sso_browser_session(
                _actor(user_id),
                principal,
                {
                    "iss": principal.issuer,
                    "sub": principal.subject,
                    "sid": principal.claims["sid"],
                    "identity_provider": "workforce",
                },
                _tokens(),
            )

        async def seed_admin_and_fence(epoch: uuid.UUID, suffix: str) -> None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET is_admin = TRUE WHERE id = $1",
                    user_id,
                )
                await conn.execute(
                    """
                    INSERT INTO admin_browser_sessions (
                        session_epoch, token_hash, csrf_token_hash, user_id,
                        external_identity_id, identity_issuer, identity_subject,
                        keycloak_sid, expires_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8,
                        NOW() + INTERVAL '5 minutes'
                    )
                    """,
                    epoch,
                    uuid.uuid4().hex * 2,
                    uuid.uuid4().hex * 2,
                    user_id,
                    identity_id,
                    principal.issuer,
                    principal.subject,
                    f"admin-{suffix}",
                )
                await conn.execute(
                    """
                    INSERT INTO sso_browser_logout_fences (
                        session_epoch, identity_issuer, keycloak_sid,
                        identity_subject, logout_issued_at, expires_at
                    ) VALUES (
                        $1, $2, $3, $4, NOW(), NOW() + INTERVAL '5 minutes'
                    )
                    """,
                    epoch,
                    principal.issuer,
                    f"fence-{suffix}",
                    principal.subject,
                )

        original = await issue_ordinary()
        await seed_admin_and_fence(_SSO_SESSION_EPOCH, "initial")
        unchanged = await epoch_service.reconcile_sso_session_epoch()
        assert unchanged.changed is False
        assert (await service.resolve_sso_browser_session(original.token)).user_id == str(user_id)

        monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
        monkeypatch.setattr(settings, "auth_runtime_generation", 2, raising=False)
        local = await epoch_service.reconcile_sso_session_epoch()
        assert local.auth_mode_changed is True
        assert (
            local.ordinary_sessions_revoked,
            local.admin_sessions_revoked,
            local.logout_fences_revoked,
        ) == (1, 1, 1)

        # Model one draining old SSO replica using its prior generation after
        # the local-mode startup transaction committed. The runtime-state
        # share lock rejects it before a write, and the FK independently
        # prevents that epoch from being reintroduced while local mode owns
        # the singleton row.
        monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
        with pytest.raises(AuthenticationError, match="authority"):
            await issue_ordinary()
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO sso_browser_logout_fences (
                        session_epoch, identity_issuer, keycloak_sid,
                        logout_issued_at, expires_at
                    ) VALUES (
                        $1, $2, 'stale-draining-sid',
                        NOW(), NOW() + INTERVAL '5 minutes'
                    )
                    """,
                    _SSO_SESSION_EPOCH,
                    principal.issuer,
                )

        # Returning to SSO remains an explicit mode boundary even when the
        # operator reuses the same UUID. No stale row can survive or be added.
        monkeypatch.setattr(settings, "auth_runtime_generation", 3, raising=False)
        resumed = await epoch_service.reconcile_sso_session_epoch()
        assert resumed.auth_mode_changed is True
        assert (
            resumed.ordinary_sessions_revoked,
            resumed.admin_sessions_revoked,
            resumed.logout_fences_revoked,
        ) == (0, 0, 0)
        with pytest.raises(AuthenticationError):
            await service.resolve_sso_browser_session(original.token)

        prior_epoch = await issue_ordinary()
        await seed_admin_and_fence(_SSO_SESSION_EPOCH, "rotation")
        next_epoch = uuid.UUID("f243032e-8f74-42af-93e9-911222e4e748")
        monkeypatch.setattr(settings, "auth_runtime_generation", 4, raising=False)
        monkeypatch.setattr(settings, "sso_session_epoch", next_epoch, raising=False)
        rotated = await epoch_service.reconcile_sso_session_epoch()
        assert rotated.auth_mode_changed is False
        assert rotated.epoch_changed is True
        assert (
            rotated.ordinary_sessions_revoked,
            rotated.admin_sessions_revoked,
            rotated.logout_fences_revoked,
        ) == (1, 1, 1)
        with pytest.raises(AuthenticationError):
            await service.resolve_sso_browser_session(prior_epoch.token)

        async with pool.acquire() as conn:
            state = await conn.fetchrow(
                "SELECT auth_mode, sso_session_epoch FROM auth_runtime_state"
            )
            assert tuple(state) == ("sso", next_epoch)
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_sessions") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM admin_browser_sessions") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_logout_fences") == 0
            audit_payload = await conn.fetchval(
                """
                SELECT string_agg(payload::TEXT, ' ' ORDER BY id)
                  FROM events
                 WHERE kind = 'auth.runtime_boundary_changed'
                """
            )
        assert audit_payload is not None
        assert str(_SSO_SESSION_EPOCH) not in audit_payload
        assert str(next_epoch) not in audit_payload
