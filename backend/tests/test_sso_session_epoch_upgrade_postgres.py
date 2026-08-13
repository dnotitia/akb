"""PostgreSQL regressions for the SSO runtime-generation upgrade boundary."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_EXACT_BASE_INIT_SQL = (
    _BACKEND / "tests" / "fixtures" / "sso_session_epoch" / "exact_base_0860a0e_init.sql"
).read_text()
_EXACT_BASE_INIT_SHA256 = "410c67e62b63f254004430cd3bb2ab72ff0fc9e4af3415ac8218fec6137696b1"
_MIGRATION_REGISTRY = (_BACKEND / "app" / "db" / "postgres.py").read_text().split("for filename in (", 1)[1]
_MIGRATION_REGISTRY = _MIGRATION_REGISTRY.split("    ):", 1)[0]
_CURRENT_MIGRATIONS = re.findall(r'"([0-9]{3}_[^"]+\.py)"', _MIGRATION_REGISTRY)
_EPOCH_MIGRATION_POSITION = _CURRENT_MIGRATIONS.index("076_sso_session_epoch.py")
_EXACT_BASE_MIGRATIONS = _CURRENT_MIGRATIONS[:_EPOCH_MIGRATION_POSITION]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_EPOCH_0 = uuid.UUID("09f2df7b-8658-45f1-b82d-a244279e3c24")
_EPOCH_1 = uuid.UUID("db123b46-ed25-4939-909e-79ae61f729e3")
_EPOCH_2 = uuid.UUID("b57ee773-a9c2-427a-b1ba-b7f171d08f45")


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
async def _init_db_regression_database(*, exact_base: bool):
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    database = f"akb_sso_epoch_init_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f'CREATE DATABASE "{database}"')
    pool: asyncpg.Pool | None = None
    try:
        target_dsn = _dsn_for_database(_DSN, database)
        if exact_base:
            conn = await asyncpg.connect(target_dsn)
            try:
                await conn.execute(_EXACT_BASE_INIT_SQL)
                await conn.executemany(
                    "INSERT INTO schema_migrations (filename) VALUES ($1)",
                    [(filename,) for filename in _EXACT_BASE_MIGRATIONS],
                )
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


async def _run_current_init_db(monkeypatch, pool) -> None:
    from app.db import postgres

    async def get_test_pool():
        return pool

    monkeypatch.setattr(postgres, "get_pool", get_test_pool)
    await postgres.init_db(max_retries=1, delay=0)


async def _browser_session_index_columns(pool) -> dict[str, list[str]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = ANY($1::TEXT[])
            """,
            ["idx_sso_browser_sessions_sid", "idx_sso_browser_sessions_subject"],
        )
    columns: dict[str, list[str]] = {}
    for row in rows:
        column_list = row["indexdef"].rsplit("(", 1)[1].removesuffix(")")
        columns[row["indexname"]] = column_list.split(", ")
    return columns


async def test_current_init_db_upgrades_exact_base_schema_and_replaces_indexes(
    monkeypatch,
) -> None:
    assert hashlib.sha256(_EXACT_BASE_INIT_SQL.encode()).hexdigest() == _EXACT_BASE_INIT_SHA256
    assert _EXACT_BASE_MIGRATIONS[-1] == "075_sso_callback_receipt.py"

    async with _init_db_regression_database(exact_base=True) as pool:
        async with pool.acquire() as conn:
            assert not await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name = 'sso_browser_sessions'
                       AND column_name = 'session_epoch'
                )
                """
            )
        assert await _browser_session_index_columns(pool) == {
            "idx_sso_browser_sessions_sid": ["identity_issuer", "keycloak_sid"],
            "idx_sso_browser_sessions_subject": ["identity_issuer", "identity_subject"],
        }

        await _run_current_init_db(monkeypatch, pool)

        assert await _browser_session_index_columns(pool) == {
            "idx_sso_browser_sessions_sid": ["session_epoch", "identity_issuer", "keycloak_sid"],
            "idx_sso_browser_sessions_subject": ["session_epoch", "identity_issuer", "identity_subject"],
        }


async def test_current_init_db_fresh_schema_finishes_with_epoch_indexes(monkeypatch) -> None:
    async with _init_db_regression_database(exact_base=False) as pool:
        await _run_current_init_db(monkeypatch, pool)

        assert await _browser_session_index_columns(pool) == {
            "idx_sso_browser_sessions_sid": ["session_epoch", "identity_issuer", "keycloak_sid"],
            "idx_sso_browser_sessions_subject": ["session_epoch", "identity_issuer", "identity_subject"],
        }


@asynccontextmanager
async def _pre_epoch_bridge_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    database = f"akb_sso_epoch_upgrade_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f'CREATE DATABASE "{database}"')
    pool: asyncpg.Pool | None = None
    try:
        target_dsn = _dsn_for_database(_DSN, database)
        conn = await asyncpg.connect(target_dsn)
        try:
            await conn.execute(_INIT_SQL)
            await conn.execute(
                """
                DROP TABLE sso_browser_sessions, sso_browser_logout_fences,
                           admin_browser_sessions, auth_runtime_state
                """
            )
            await conn.execute("DROP TABLE IF EXISTS auth_runtime_epoch_upgrade")
            from app.db.postgres import _load_migration

            admin_migration = _load_migration("072_admin_browser_sessions.py")
            browser_migration = _load_migration("074_sso_browser_sessions.py")
            epoch_migration = _load_migration("076_sso_session_epoch.py")
            events_migration = _load_migration("015_events_outbox.py")
            assert admin_migration is not None
            assert browser_migration is not None
            assert epoch_migration is not None
            assert events_migration is not None
            await events_migration.migrate(conn=conn)
            await admin_migration.migrate(conn=conn)
            await browser_migration.migrate(conn=conn)
            await epoch_migration.migrate(conn=conn)
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(target_dsn, min_size=1, max_size=12)
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


async def _seed_identity(pool) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, is_admin,
                auth_provider, account_status, account_kind
            ) VALUES (
                $1, $2, $3, '!sso!', TRUE,
                'keycloak', 'active', 'human'
            )
            """,
            user_id,
            f"user-{user_id.hex[:8]}",
            f"{user_id.hex[:8]}@example.com",
        )
        await conn.execute(
            """
            INSERT INTO external_identities (id, user_id, issuer, subject)
            VALUES ($1, $2, 'https://id.example/realms/akb', $3)
            """,
            identity_id,
            user_id,
            f"subject-{user_id.hex[:8]}",
        )
    return user_id, identity_id


async def _insert_legacy_admin(
    pool,
    user_id: uuid.UUID,
    identity_id: uuid.UUID,
    *,
    token: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_browser_sessions (
                token_hash, csrf_token_hash, user_id, external_identity_id,
                identity_issuer, identity_subject, keycloak_sid, expires_at
            ) VALUES (
                $1, $2, $3, $4, 'https://id.example/realms/akb', $5, $6,
                NOW() + INTERVAL '5 minutes'
            )
            """,
            hashlib.sha256(token.encode()).hexdigest(),
            "a" * 64,
            user_id,
            identity_id,
            f"subject-{user_id.hex[:8]}",
            f"admin-{token[-8:]}",
        )


async def _insert_legacy_browser(
    pool,
    user_id: uuid.UUID,
    identity_id: uuid.UUID,
    *,
    token: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sso_browser_sessions (
                id, token_hash, csrf_token_hash, user_id, external_identity_id,
                identity_issuer, identity_subject, keycloak_sid, token_envelope,
                access_expires_at, refresh_expires_at, idle_expires_at,
                absolute_expires_at
            ) VALUES (
                $1, $2, $3, $4, $5, 'https://id.example/realms/akb', $6, $7,
                $8, NOW() + INTERVAL '5 minutes', NOW() + INTERVAL '5 minutes',
                NOW() + INTERVAL '5 minutes', NOW() + INTERVAL '5 minutes'
            )
            """,
            uuid.uuid4(),
            hashlib.sha256(token.encode()).hexdigest(),
            "b" * 64,
            user_id,
            identity_id,
            f"subject-{user_id.hex[:8]}",
            f"browser-{token[-8:]}",
            "legacy-envelope-is-never-accepted-0000",
        )


async def _insert_legacy_fence(pool, *, suffix: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sso_browser_logout_fences (
                identity_issuer, keycloak_sid, logout_issued_at, expires_at
            ) VALUES (
                'https://id.example/realms/akb', $1, NOW(),
                NOW() + INTERVAL '5 minutes'
            )
            """,
            f"legacy-fence-{suffix}",
        )


async def _legacy_admin_resolves(pool, token: str) -> bool:
    """Mirror the pre-epoch image's token-only lookup."""
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT TRUE
                  FROM admin_browser_sessions
                 WHERE token_hash = $1 AND expires_at > NOW()
                """,
                hashlib.sha256(token.encode()).hexdigest(),
            )
        )


def _configure(monkeypatch, epoch_service, pool) -> None:
    from app.config import settings

    async def get_test_pool():
        return pool

    monkeypatch.setattr(epoch_service, "get_pool", get_test_pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "auth_runtime_generation", 1, raising=False)
    monkeypatch.setattr(settings, "sso_session_epoch", _EPOCH_0, raising=False)
    monkeypatch.setattr(settings, "sso_session_epoch_upgrade", None, raising=False)


async def test_mixed_version_upgrade_guard_and_rollback_are_executable(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.exceptions import AuthenticationError
    from app.services import admin_auth_service
    from app.services import sso_session_epoch as epoch_service

    async with _pre_epoch_bridge_database() as pool:
        _configure(monkeypatch, epoch_service, pool)
        user_id, identity_id = await _seed_identity(pool)
        legacy_admin_token = "legacy-admin-session-token-value"
        await _insert_legacy_admin(
            pool,
            user_id,
            identity_id,
            token=legacy_admin_token,
        )
        await _insert_legacy_browser(
            pool,
            user_id,
            identity_id,
            token="legacy-browser-session-token-value",
        )
        await _insert_legacy_fence(pool, suffix="before-upgrade")
        assert await _legacy_admin_resolves(pool, legacy_admin_token) is True

        async def get_test_pool():
            return pool

        monkeypatch.setattr(admin_auth_service, "get_pool", get_test_pool)
        with pytest.raises(AuthenticationError):
            await admin_auth_service.resolve_sso_admin_browser_session(legacy_admin_token)

        async with pool.acquire() as conn:
            nullable = await conn.fetch(
                """
                SELECT table_name, is_nullable
                  FROM information_schema.columns
                 WHERE column_name = 'session_epoch'
                   AND table_name = ANY($1::TEXT[])
                 ORDER BY table_name
                """,
                [
                    "admin_browser_sessions",
                    "sso_browser_logout_fences",
                    "sso_browser_sessions",
                ],
            )
            assert [tuple(row) for row in nullable] == [
                ("admin_browser_sessions", "YES"),
                ("sso_browser_logout_fences", "YES"),
                ("sso_browser_sessions", "YES"),
            ]

        with pytest.raises(RuntimeError, match="stop-the-world-v1"):
            await epoch_service.reconcile_sso_session_epoch()

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM admin_browser_sessions") == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_sessions") == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_logout_fences") == 1

        monkeypatch.setattr(
            settings,
            "sso_session_epoch_upgrade",
            "stop-the-world-v1",
            raising=False,
        )
        with pytest.raises(RuntimeError, match="quiescent preflight"):
            await epoch_service.reconcile_sso_session_epoch()
        activated = await epoch_service.reconcile_sso_session_epoch(upgrade_preflight=True)
        assert activated.changed is True
        assert (
            activated.admin_sessions_revoked,
            activated.ordinary_sessions_revoked,
            activated.logout_fences_revoked,
        ) == (1, 1, 1)

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT state FROM auth_runtime_epoch_upgrade WHERE singleton") == "enforced"

        for legacy_write in (
            _insert_legacy_admin(
                pool,
                user_id,
                identity_id,
                token="legacy-admin-after-cutover-value",
            ),
            _insert_legacy_browser(
                pool,
                user_id,
                identity_id,
                token="legacy-browser-after-cutover-value",
            ),
            _insert_legacy_fence(pool, suffix="after-cutover"),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await legacy_write

        await epoch_service.prepare_sso_session_epoch_rollback()
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT state FROM auth_runtime_epoch_upgrade WHERE singleton") == "rollback_ready"
            )
            assert await conn.fetchval("SELECT COUNT(*) FROM auth_runtime_state") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM admin_browser_sessions") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_sessions") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM sso_browser_logout_fences") == 0

        await _insert_legacy_admin(
            pool,
            user_id,
            identity_id,
            token=legacy_admin_token,
        )
        assert await _legacy_admin_resolves(pool, legacy_admin_token) is True
        with pytest.raises(AuthenticationError):
            await admin_auth_service.resolve_sso_admin_browser_session(legacy_admin_token)
        with pytest.raises(RuntimeError, match="stale"):
            await epoch_service.reconcile_sso_session_epoch(upgrade_preflight=True)


async def test_runtime_generation_is_monotonic_exact_and_stale_proof(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.services import sso_session_epoch as epoch_service
    from app.services.sso_session_epoch import AuthRuntimeBoundary

    async with _pre_epoch_bridge_database() as pool:
        _configure(monkeypatch, epoch_service, pool)
        monkeypatch.setattr(
            settings,
            "sso_session_epoch_upgrade",
            "stop-the-world-v1",
            raising=False,
        )
        first = await epoch_service.reconcile_sso_session_epoch(upgrade_preflight=True)
        assert first.changed is True

        exact = await epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(1, "sso", _EPOCH_0))
        assert exact.changed is False

        local = await epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(2, "local", None))
        assert local.auth_mode_changed is True

        for stale_or_conflicting in (
            AuthRuntimeBoundary(1, "sso", _EPOCH_0),
            AuthRuntimeBoundary(2, "sso", _EPOCH_0),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await epoch_service.reconcile_sso_session_epoch(stale_or_conflicting)
            assert str(_EPOCH_0) not in str(exc_info.value)

        resumed = await epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(3, "sso", _EPOCH_0))
        assert resumed.auth_mode_changed is True
        rotated = await epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(4, "sso", _EPOCH_1))
        assert rotated.epoch_changed is True

        out_of_order = await asyncio.gather(
            epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(5, "local", None)),
            epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(6, "sso", _EPOCH_2)),
            return_exceptions=True,
        )
        assert not any(isinstance(item, asyncpg.DeadlockDetectedError) for item in out_of_order)
        async with pool.acquire() as conn:
            state = await conn.fetchrow(
                """
                SELECT runtime_generation, auth_mode, sso_session_epoch
                  FROM auth_runtime_state
                """
            )
        assert tuple(state) == (6, "sso", _EPOCH_2)

        simultaneous_conflict = await asyncio.gather(
            epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(7, "local", None)),
            epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(7, "sso", _EPOCH_1)),
            return_exceptions=True,
        )
        assert sum(isinstance(item, RuntimeError) for item in simultaneous_conflict) == 1
        async with pool.acquire() as conn:
            state = await conn.fetchrow(
                """
                SELECT runtime_generation, auth_mode, sso_session_epoch
                  FROM auth_runtime_state
                """
            )
            payloads = await conn.fetch(
                """
                SELECT payload
                  FROM events
                 WHERE kind = 'auth.runtime_boundary_changed'
                """
            )
        assert state["runtime_generation"] == 7
        assert (state["auth_mode"], state["sso_session_epoch"]) in {
            ("local", None),
            ("sso", _EPOCH_1),
        }
        assert all("runtime_generation" not in row["payload"] for row in payloads)
        serialized_payloads = " ".join(str(row["payload"]) for row in payloads)
        for epoch in (_EPOCH_0, _EPOCH_1, _EPOCH_2):
            assert str(epoch) not in serialized_payloads


async def test_concurrent_migration_and_runtime_starts_do_not_deadlock(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.db.postgres import _load_migration
    from app.services import sso_session_epoch as epoch_service
    from app.services.sso_session_epoch import AuthRuntimeBoundary

    async with _pre_epoch_bridge_database() as pool:
        _configure(monkeypatch, epoch_service, pool)
        monkeypatch.setattr(
            settings,
            "sso_session_epoch_upgrade",
            "stop-the-world-v1",
            raising=False,
        )
        await epoch_service.reconcile_sso_session_epoch(upgrade_preflight=True)
        migration = _load_migration("076_sso_session_epoch.py")
        assert migration is not None

        async def rerun_migration() -> None:
            async with pool.acquire() as conn:
                await migration.migrate(conn=conn)

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                rerun_migration(),
                rerun_migration(),
                *(
                    epoch_service.reconcile_sso_session_epoch(AuthRuntimeBoundary(1, "sso", _EPOCH_0))
                    for _ in range(12)
                ),
                return_exceptions=True,
            ),
            timeout=15,
        )
        assert not any(isinstance(item, asyncpg.DeadlockDetectedError) for item in outcomes), outcomes
        assert not any(isinstance(item, BaseException) for item in outcomes), outcomes
