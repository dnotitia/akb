"""PostgreSQL contracts for explicit recovery-admin provisioning."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
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
_LOCAL_PASSWORD = "local-recovery-password"  # pragma: allowlist secret
_CONCURRENT_PASSWORD = "concurrent-recovery-password"  # pragma: allowlist secret


def _dsn_for_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(parsed._replace(path=f"/{database}"))


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    database = f"akb_recovery_admin_{uuid.uuid4().hex[:12]}"
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
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


class _RoleSync:
    def __init__(self):
        self.created: list[uuid.UUID] = []
        self.deleted: list[uuid.UUID] = []

    async def on_user_create(self, user_id):
        self.created.append(uuid.UUID(str(user_id)))

    async def on_user_delete(self, user_id):
        self.deleted.append(uuid.UUID(str(user_id)))


@pytest.fixture
async def services(monkeypatch):
    async with _fresh_database() as pool:
        from app.services import (
            access_service,
            account_service,
            auth_service,
            recovery_admin_service,
        )

        role_sync = _RoleSync()

        async def _get_pool():
            return pool

        for module in (
            access_service,
            account_service,
            auth_service,
            recovery_admin_service,
        ):
            monkeypatch.setattr(module, "get_pool", _get_pool)
        for module in (access_service, account_service, auth_service, recovery_admin_service):
            monkeypatch.setattr(module, "get_role_sync", lambda: role_sync)

        yield pool, role_sync, recovery_admin_service, account_service, access_service, auth_service


async def test_fresh_and_populated_local_registration_are_non_admin(services, monkeypatch):
    pool, _, _, _, _, auth_service = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    first = await auth_service.register(
        f"ordinary-{uuid.uuid4().hex[:8]}",
        f"ordinary-{uuid.uuid4().hex[:8]}@example.com",
        "ordinary-password-one",
    )
    second = await auth_service.register(
        f"ordinary-{uuid.uuid4().hex[:8]}",
        f"ordinary-{uuid.uuid4().hex[:8]}@example.com",
        "ordinary-password-two",
    )

    assert first["is_admin"] is False
    assert second["is_admin"] is False
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_admin") == 0


async def test_local_provisioning_empty_database_and_exact_repeat_converge(services, monkeypatch):
    pool, role_sync, service, _, _, auth_service = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    kwargs = {
        "username": "recovery-admin",
        "email": "recovery-admin@example.com",
        "password": _LOCAL_PASSWORD,
    }

    first = await service.provision_local_recovery_admin(**kwargs)
    repeated = await service.provision_local_recovery_admin(**kwargs)

    assert first["created"] is True
    assert repeated["created"] is False
    assert repeated["user_id"] == first["user_id"]
    assert first["is_admin"] is True
    assert first["is_recovery_admin"] is True
    assert role_sync.created == [uuid.UUID(first["user_id"])]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT username, email, password_hash, is_admin, is_recovery_admin,
                   auth_provider, account_kind
              FROM users WHERE id = $1
            """,
            uuid.UUID(first["user_id"]),
        )
        identity_count = await conn.fetchval(
            "SELECT COUNT(*) FROM external_identities WHERE user_id = $1",
            uuid.UUID(first["user_id"]),
        )
    assert row["username"] == kwargs["username"]
    assert row["email"] == kwargs["email"]
    assert row["is_admin"] is True
    assert row["is_recovery_admin"] is True
    assert row["auth_provider"] == "local"
    assert row["account_kind"] == "human"
    assert auth_service.verify_password(kwargs["password"], row["password_hash"])
    assert identity_count == 0


async def test_local_provisioning_never_adopts_an_existing_email(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminConflictError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    async with pool.acquire() as conn:
        ordinary_id = await conn.fetchval(
            """
            INSERT INTO users (username, email, password_hash, is_admin)
            VALUES ('ordinary', 'claimed@example.com', '!existing!', false)
            RETURNING id
            """
        )

    with pytest.raises(RecoveryAdminConflictError):
        await service.provision_local_recovery_admin(
            username="recovery-admin",
            email="claimed@example.com",
            password=_LOCAL_PASSWORD,
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_admin, is_recovery_admin FROM users WHERE id = $1",
            ordinary_id,
        )
    assert dict(row) == {"is_admin": False, "is_recovery_admin": False}


async def test_concurrent_exact_local_provisioning_creates_one_identity(services, monkeypatch):
    pool, role_sync, service, _, _, auth_service = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    password = _CONCURRENT_PASSWORD
    password_hash = auth_service.hash_password(password)

    async def _hash_once(_password):
        return password_hash

    monkeypatch.setattr(service, "hash_password_async", _hash_once)
    results = await asyncio.gather(
        *[
            service.provision_local_recovery_admin(
                username="recovery-admin",
                email="recovery-admin@example.com",
                password=password,
            )
            for _ in range(4)
        ]
    )

    assert sum(result["created"] for result in results) == 1
    assert len({result["user_id"] for result in results}) == 1
    assert len(role_sync.created) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users") == 1


async def test_concurrent_conflicting_designations_fail_closed(services, monkeypatch):
    pool, _, service, _, _, auth_service = services
    from app.config import settings
    from app.exceptions import RecoveryAdminConflictError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    password_hash = auth_service.hash_password(_CONCURRENT_PASSWORD)

    async def _hash_once(_password):
        return password_hash

    monkeypatch.setattr(service, "hash_password_async", _hash_once)
    results = await asyncio.gather(
        service.provision_local_recovery_admin(
            username="recovery-one",
            email="recovery-one@example.com",
            password=_CONCURRENT_PASSWORD,
        ),
        service.provision_local_recovery_admin(
            username="recovery-two",
            email="recovery-two@example.com",
            password=_CONCURRENT_PASSWORD,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, RecoveryAdminConflictError) for result in results) == 1
    assert sum(isinstance(result, dict) and result["created"] for result in results) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_recovery_admin") == 1


async def test_sso_prebinding_is_exact_idempotent_and_has_no_local_password(services, monkeypatch):
    pool, role_sync, service, _, _, auth_service = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    kwargs = {
        "username": "sso-recovery-admin",
        "email": "sso-recovery-admin@example.com",
        "issuer": "https://issuer.example.com/realms/akb",
        "subject": "stable-admin-subject",
    }

    first = await service.provision_sso_recovery_admin(**kwargs)
    repeated = await service.provision_sso_recovery_admin(**kwargs)

    assert first["created"] is True
    assert repeated["created"] is False
    assert repeated["user_id"] == first["user_id"]
    assert role_sync.created == [uuid.UUID(first["user_id"])]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.password_hash, u.auth_provider, u.is_admin,
                   u.is_recovery_admin, e.issuer, e.subject,
                   e.username_snapshot, e.email_snapshot
              FROM users u
              JOIN external_identities e ON e.user_id = u.id
             WHERE u.id = $1
            """,
            uuid.UUID(first["user_id"]),
        )
    assert row["auth_provider"] == "keycloak"
    assert row["is_admin"] is True
    assert row["is_recovery_admin"] is True
    assert row["issuer"] == kwargs["issuer"]
    assert row["subject"] == kwargs["subject"]
    assert row["username_snapshot"] == kwargs["username"]
    assert row["email_snapshot"] == kwargs["email"]
    assert auth_service.verify_password("any-local-password", row["password_hash"]) is False


async def test_sso_prebinding_never_links_an_email_or_existing_binding(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminConflictError

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    existing_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, is_admin)
            VALUES ($1, 'ordinary', 'claimed@example.com', '!existing!', false)
            """,
            existing_id,
        )
        await conn.execute(
            """
            INSERT INTO external_identities (user_id, issuer, subject, email_snapshot)
            VALUES ($1, 'https://issuer.example.com/realms/akb', 'claimed-subject',
                    'claimed@example.com')
            """,
            existing_id,
        )

    attempts = (
        {
            "username": "new-recovery",
            "email": "claimed@example.com",
            "issuer": "https://other-issuer.example.com/realms/akb",
            "subject": "new-subject",
        },
        {
            "username": "another-recovery",
            "email": "another@example.com",
            "issuer": "https://issuer.example.com/realms/akb",
            "subject": "claimed-subject",
        },
    )
    for kwargs in attempts:
        with pytest.raises(RecoveryAdminConflictError):
            await service.provision_sso_recovery_admin(**kwargs)

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users") == 1
        assert await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_admin") == 0


async def test_wrong_mode_rejects_before_password_or_database(services, monkeypatch):
    _, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminModeError

    async def _must_not_hash(_password):
        raise AssertionError("wrong-mode provisioning must reject before password work")

    async def _must_not_get_pool():
        raise AssertionError("wrong-mode provisioning must reject before database access")

    monkeypatch.setattr(service, "hash_password_async", _must_not_hash)
    monkeypatch.setattr(service, "get_pool", _must_not_get_pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    with pytest.raises(RecoveryAdminModeError):
        await service.provision_local_recovery_admin(
            username="recovery-admin",
            email="recovery-admin@example.com",
            password=_LOCAL_PASSWORD,
        )

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    with pytest.raises(RecoveryAdminModeError):
        await service.provision_sso_recovery_admin(
            username="recovery-admin",
            email="recovery-admin@example.com",
            issuer="https://issuer.example.com/realms/akb",
            subject="stable-admin-subject",
        )


async def test_recovery_admin_cannot_be_demoted_or_deleted(services, monkeypatch):
    pool, _, service, account_service, access_service, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminProtectedError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )

    with pytest.raises(RecoveryAdminProtectedError):
        await account_service.set_user_admin(
            provisioned["user_id"],
            is_admin=False,
            actor_id="operator",
        )
    with pytest.raises(RecoveryAdminProtectedError):
        await access_service.delete_user_account(provisioned["user_id"])

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_admin, is_recovery_admin FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    assert dict(row) == {"is_admin": True, "is_recovery_admin": True}


async def test_normal_admin_can_still_be_demoted_and_deleted(services):
    pool, role_sync, _, account_service, access_service, _ = services
    user_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, is_admin)
            VALUES ($1, 'ordinary-admin', 'ordinary-admin@example.com', '!hash!', true)
            """,
            user_id,
        )

    demoted = await account_service.set_user_admin(
        str(user_id),
        is_admin=False,
        actor_id="operator",
    )
    deleted = await access_service.delete_user_account(str(user_id))

    assert demoted["is_admin"] is False
    assert deleted["deleted"] is True
    assert role_sync.deleted == [user_id]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users WHERE id = $1", user_id) == 0


async def test_migration_upgrades_old_schema_and_is_idempotent(services):
    pool, _, _, _, _, _ = services
    from app.db.postgres import _load_migration

    async with pool.acquire() as conn:
        await conn.execute("DROP INDEX users_one_recovery_admin")
        await conn.execute(
            "ALTER TABLE users DROP CONSTRAINT users_recovery_admin_requires_admin"
        )
        await conn.execute("ALTER TABLE users DROP COLUMN is_recovery_admin")
        await conn.execute("ALTER TABLE external_identities DROP COLUMN username_snapshot")

        migration = _load_migration("071_recovery_admin.py")
        assert migration is not None
        await migration.migrate(conn)
        await migration.migrate(conn)

        user_column = await conn.fetchval(
            """
            SELECT is_nullable = 'NO'
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'users'
               AND column_name = 'is_recovery_admin'
            """
        )
        snapshot_column = await conn.fetchval(
            """
            SELECT is_nullable = 'YES'
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'external_identities'
               AND column_name = 'username_snapshot'
            """
        )
        constraint = await conn.fetchval(
            """
            SELECT COUNT(*) FROM pg_constraint
             WHERE conrelid = 'users'::regclass
               AND conname = 'users_recovery_admin_requires_admin'
            """
        )
        unique_index = await conn.fetchval(
            """
            SELECT COUNT(*) FROM pg_indexes
             WHERE schemaname = 'public' AND tablename = 'users'
               AND indexname = 'users_one_recovery_admin'
            """
        )
        recovery_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, is_admin, is_recovery_admin
            ) VALUES ($1, 'migration-recovery', 'migration-recovery@example.com',
                      '!hash!', true, true)
            """,
            recovery_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE users SET is_admin = false WHERE id = $1",
                recovery_id,
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO users (
                    username, email, password_hash, is_admin, is_recovery_admin
                ) VALUES ('migration-recovery-two',
                          'migration-recovery-two@example.com', '!hash!', true, true)
                """
            )

    assert user_column is True
    assert snapshot_column is True
    assert constraint == 1
    assert unique_index == 1
