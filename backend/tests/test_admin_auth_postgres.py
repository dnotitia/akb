"""PostgreSQL proof for exact admin binding and opaque session custody.

This file is pinned in the live-PostgreSQL CI job.  The ordinary unit job has
no database and would otherwise skip every assertion here, including the 072
fresh-schema/upgrade-schema catalog-equivalence check.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


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


async def _admin_schema_shape(conn) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Return the database-owned shape introduced by migration 072."""
    columns = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'admin_browser_sessions'
         ORDER BY ordinal_position
        """
    )
    constraints = await conn.fetch(
        """
        SELECT conname, contype, pg_get_constraintdef(oid), convalidated
          FROM pg_constraint
         WHERE conrelid = 'admin_browser_sessions'::regclass
         ORDER BY conname
        """
    )
    indexes = await conn.fetch(
        """
        SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid), indisvalid
          FROM pg_index
         WHERE indrelid = 'admin_browser_sessions'::regclass
            OR indexrelid = 'external_identities_id_user_key'::regclass
         ORDER BY indexrelid::regclass::text
        """
    )
    return {
        "columns": tuple(tuple(row) for row in columns),
        "constraints": tuple(tuple(row) for row in constraints),
        "indexes": tuple(tuple(row) for row in indexes),
    }


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    database = f"akb_admin_auth_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f'CREATE DATABASE "{database}"')
    pool: asyncpg.Pool | None = None
    try:
        target_dsn = _dsn_for_database(_DSN, database)
        conn = await asyncpg.connect(target_dsn)
        try:
            await conn.execute(_INIT_SQL)
            from app.db.postgres import _load_migration

            fresh_admin_shape = await _admin_schema_shape(conn)
            await conn.execute("DROP TABLE admin_browser_sessions")
            await conn.execute("DROP INDEX external_identities_id_user_key")
            admin_session_migration = _load_migration("072_admin_browser_sessions.py")
            assert admin_session_migration is not None
            await admin_session_migration.migrate(conn=conn)
            await admin_session_migration.migrate(conn=conn)
            assert await _admin_schema_shape(conn) == fresh_admin_shape

            events_migration = _load_migration("015_events_outbox.py")
            assert events_migration is not None
            await events_migration.migrate(conn=conn)
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(target_dsn, min_size=1, max_size=4)
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


async def test_sso_admin_is_exact_prebound_opaque_and_live_rechecked(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.exceptions import AuthenticationError, ForbiddenError, MembershipRequiredError
    from app.services import admin_auth_service, keycloak_oidc

    async with _fresh_database() as pool:

        async def get_test_pool():
            return pool

        monkeypatch.setattr(admin_auth_service, "get_pool", get_test_pool)
        monkeypatch.setattr(keycloak_oidc, "get_pool", get_test_pool)
        monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
        monkeypatch.setattr(
            settings,
            "keycloak_server_url",
            "https://id.example",
            raising=False,
        )
        monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
        monkeypatch.setattr(settings, "keycloak_admin_client_id", "akb-admin", raising=False)
        monkeypatch.setattr(settings, "public_base_url", "https://akb.example", raising=False)
        monkeypatch.setattr(settings, "admin_browser_session_ttl_secs", 300, raising=False)

        oidc = keycloak_oidc.KeycloakOIDC()
        rejected_request = await oidc.begin_admin_login()
        rejected_state = parse_qs(urlsplit(rejected_request.location).query)["state"][0]
        assert await keycloak_oidc._store_consume(rejected_state, "ordinary-state") is None
        assert (
            await oidc.consume_admin_state(
                rejected_state,
                "wrong-browser-binding-value-that-is-long-enough",
            )
            is None
        )
        rejected_transient = await oidc.consume_admin_state(
            rejected_state,
            rejected_request.browser_binding,
        )
        assert rejected_transient is not None
        assert set(rejected_transient) == {"nonce", "code_verifier"}
        assert await oidc.consume_admin_state(rejected_state, rejected_request.browser_binding) is None

        accepted_request = await oidc.begin_admin_login()
        accepted_state = parse_qs(urlsplit(accepted_request.location).query)["state"][0]
        accepted_transient = await oidc.consume_admin_state(
            accepted_state,
            accepted_request.browser_binding,
        )
        assert accepted_transient is not None
        assert set(accepted_transient) == {"nonce", "code_verifier"}
        assert await oidc.consume_admin_state(accepted_state, accepted_request.browser_binding) is None

        admin_user_id = uuid.uuid4()
        member_user_id = uuid.uuid4()
        admin_identity_id = uuid.uuid4()
        member_identity_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO users (
                    id, username, email, password_hash, is_admin,
                    auth_provider, account_status, account_kind
                ) VALUES ($1, $2, $3, '!sso!', $4, 'keycloak', 'active', 'human')
                """,
                [
                    (admin_user_id, "admin", "admin@example.com", True),
                    (member_user_id, "member", "member@example.com", False),
                ],
            )
            await conn.executemany(
                """
                INSERT INTO external_identities (id, user_id, issuer, subject)
                VALUES ($1, $2, $3, $4)
                """,
                [
                    (
                        admin_identity_id,
                        admin_user_id,
                        settings.keycloak_issuer,
                        "admin-subject",
                    ),
                    (
                        member_identity_id,
                        member_user_id,
                        settings.keycloak_issuer,
                        "member-subject",
                    ),
                ],
            )

        claims = {
            "iss": settings.keycloak_issuer,
            "sub": "admin-subject",
            "sid": "keycloak-session-id",
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
        }
        identity = await admin_auth_service.resolve_prebound_sso_product_admin(claims)
        assert identity.user_id == admin_user_id
        assert identity.external_identity_id == admin_identity_id

        with pytest.raises(AuthenticationError):
            await admin_auth_service.create_sso_admin_browser_session(
                identity,
                {**claims, "sub": "member-subject"},
            )

        with pytest.raises(MembershipRequiredError):
            await admin_auth_service.resolve_prebound_sso_product_admin({**claims, "sub": "unknown-subject"})
        with pytest.raises(ForbiddenError):
            await admin_auth_service.resolve_prebound_sso_product_admin({**claims, "sub": "member-subject"})
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM users") == 2
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO admin_browser_sessions (
                        token_hash, csrf_token_hash, user_id,
                        external_identity_id, identity_issuer, identity_subject,
                        keycloak_sid, expires_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6,
                        'sid', NOW() + INTERVAL '5 minutes'
                    )
                    """,
                    "a" * 64,
                    "b" * 64,
                    admin_user_id,
                    member_identity_id,
                    settings.keycloak_issuer,
                    "member-subject",
                )

        issued = await admin_auth_service.create_sso_admin_browser_session(
            identity,
            claims,
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM admin_browser_sessions WHERE user_id = $1",
                admin_user_id,
            )
            assert row is not None
            assert issued.token not in tuple(str(value) for value in row.values())
            assert issued.csrf_token not in tuple(str(value) for value in row.values())
            assert len(row["token_hash"]) == len(row["csrf_token_hash"]) == 64

        resolved = await admin_auth_service.resolve_sso_admin_browser_session(issued.token)
        assert resolved.user_id == admin_user_id

        with pytest.raises(AuthenticationError):
            await admin_auth_service.revoke_sso_admin_browser_session(
                issued.token,
                issued.csrf_token,
                "wrong-csrf-token-that-is-long-enough",
            )
        await admin_auth_service.revoke_sso_admin_browser_session(
            issued.token,
            issued.csrf_token,
            issued.csrf_token,
        )
        with pytest.raises(AuthenticationError):
            await admin_auth_service.resolve_sso_admin_browser_session(issued.token)

        concurrent_sessions = await asyncio.gather(
            *(admin_auth_service.create_sso_admin_browser_session(identity, claims) for _ in range(12))
        )
        assert len(concurrent_sessions) == 12
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_browser_sessions WHERE user_id = $1",
                    admin_user_id,
                )
                == 8
            )
            await conn.execute(
                "DELETE FROM admin_browser_sessions WHERE user_id = $1",
                admin_user_id,
            )

        replacement = await admin_auth_service.create_sso_admin_browser_session(
            identity,
            claims,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE external_identities SET issuer = $2 WHERE id = $1",
                admin_identity_id,
                "https://replacement.example/realms/akb",
            )
        with pytest.raises(AuthenticationError):
            await admin_auth_service.resolve_sso_admin_browser_session(replacement.token)
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_browser_sessions WHERE user_id = $1",
                    admin_user_id,
                )
                == 0
            )
            await conn.execute(
                "UPDATE external_identities SET issuer = $2 WHERE id = $1",
                admin_identity_id,
                settings.keycloak_issuer,
            )

        replacement = await admin_auth_service.create_sso_admin_browser_session(
            identity,
            claims,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE external_identities SET subject = $2 WHERE id = $1",
                admin_identity_id,
                "replacement-admin-subject",
            )
        with pytest.raises(AuthenticationError):
            await admin_auth_service.resolve_sso_admin_browser_session(replacement.token)
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_browser_sessions WHERE user_id = $1",
                    admin_user_id,
                )
                == 0
            )
            await conn.execute(
                "UPDATE external_identities SET subject = $2 WHERE id = $1",
                admin_identity_id,
                "admin-subject",
            )

        replacement = await admin_auth_service.create_sso_admin_browser_session(
            identity,
            claims,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_admin = false WHERE id = $1",
                admin_user_id,
            )
        with pytest.raises(AuthenticationError):
            await admin_auth_service.resolve_sso_admin_browser_session(replacement.token)
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_browser_sessions WHERE user_id = $1",
                    admin_user_id,
                )
                == 0
            )
