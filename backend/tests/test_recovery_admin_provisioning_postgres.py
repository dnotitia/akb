"""PostgreSQL contracts for explicit recovery-admin provisioning."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
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
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


class _RoleSync:
    def __init__(self):
        self.created: list[uuid.UUID] = []
        self.deleted: list[uuid.UUID] = []
        self.revoked_tokens: list[uuid.UUID] = []
        self.fail_token: uuid.UUID | None = None

    async def on_user_create(self, user_id):
        self.created.append(uuid.UUID(str(user_id)))

    async def on_user_delete(self, user_id):
        self.deleted.append(uuid.UUID(str(user_id)))

    async def revoke_token_role_strict(self, token_id):
        token_uuid = uuid.UUID(str(token_id))
        if token_uuid == self.fail_token:
            raise RuntimeError("role cleanup failed")
        self.revoked_tokens.append(token_uuid)


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


async def _create_retirement_actor(pool) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    token_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, is_admin,
                auth_provider, account_status, account_kind
            ) VALUES ($1, $2, $3, '!service:no-local-login!', true,
                      'service', 'active', 'service')
            """,
            user_id,
            f"retirement-service-{user_id.hex}",
            f"retirement-service-{user_id.hex}@service.invalid",
        )
        await conn.execute(
            """
            INSERT INTO tokens (
                id, user_id, name, token_hash, token_prefix, scopes, key_class
            ) VALUES ($1, $2, 'retirement', $3, 'akb_secret_',
                      ARRAY['read', 'write', 'admin'], 'service')
            """,
            token_id,
            user_id,
            uuid.uuid4().hex,
        )
    return user_id, token_id


async def test_retirement_is_disabled_in_local_mode_without_mutation(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminModeError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    recovery_user_id = uuid.UUID(provisioned["user_id"])
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)

    with pytest.raises(RecoveryAdminModeError):
        await service.retire_local_recovery_admin(
            expected_username="local-recovery-admin",
            expected_email="local-recovery-admin@example.com",
            actor_user_id=str(actor_user_id),
            actor_token_id=str(actor_token_id),
        )

    async with pool.acquire() as conn:
        state = await conn.fetchrow(
            "SELECT account_status, is_admin, is_recovery_admin FROM users WHERE id = $1",
            recovery_user_id,
        )
    assert dict(state) == {
        "account_status": "active",
        "is_admin": True,
        "is_recovery_admin": True,
    }


async def test_retirement_is_exact_atomic_and_idempotent(services, monkeypatch):
    pool, role_sync, service, account_service, access_service, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminProtectedError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    recovery_user_id = uuid.UUID(provisioned["user_id"])
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    target_token_ids = [uuid.uuid4(), uuid.uuid4()]
    async with pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT tokens_revoked_before FROM users WHERE id = $1",
            recovery_user_id,
        )
        await conn.executemany(
            """
            INSERT INTO tokens (
                id, user_id, name, token_hash, token_prefix, scopes, key_class
            ) VALUES ($1, $2, $3, $4, 'akb_recover', ARRAY['read', 'write'], $5)
            """,
            [
                (
                    target_token_ids[0],
                    recovery_user_id,
                    "legacy-pat",
                    uuid.uuid4().hex,
                    "pat",
                ),
                (
                    target_token_ids[1],
                    recovery_user_id,
                    "legacy-service-key",
                    uuid.uuid4().hex,
                    "service",
                ),
            ],
        )

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    first = await service.retire_local_recovery_admin(
        expected_username="local-recovery-admin",
        expected_email="local-recovery-admin@example.com",
        actor_user_id=str(actor_user_id),
        actor_token_id=str(actor_token_id),
    )
    repeated = await service.retire_local_recovery_admin(
        expected_username="local-recovery-admin",
        expected_email="local-recovery-admin@example.com",
        actor_user_id=str(actor_user_id),
        actor_token_id=str(actor_token_id),
    )

    with pytest.raises(RecoveryAdminProtectedError):
        await account_service.activate_user(
            str(recovery_user_id),
            actor_id=str(actor_user_id),
        )
    with pytest.raises(RecoveryAdminProtectedError):
        await access_service.delete_user_account(str(recovery_user_id))

    expected_proof = {
        "user_id": str(recovery_user_id),
        "username": "local-recovery-admin",
        "email": "local-recovery-admin@example.com",
        "account_status": "suspended",
        "is_admin": False,
        "is_recovery_admin": False,
        "account_kind": "human",
        "auth_provider": "local",
    }
    assert first == expected_proof
    assert repeated == expected_proof
    assert set(role_sync.revoked_tokens) == set(target_token_ids)

    async with pool.acquire() as conn:
        state = await conn.fetchrow(
            """
            SELECT password_hash, account_status, is_admin, is_recovery_admin,
                   account_kind, auth_provider, tokens_revoked_before,
                   (SELECT COUNT(*) FROM tokens WHERE user_id = users.id) AS token_count,
                   (SELECT COUNT(*) FROM external_identities WHERE user_id = users.id) AS identity_count
              FROM users WHERE id = $1
            """,
            recovery_user_id,
        )
        cleanup_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM account_token_cleanup
             WHERE user_id = $1 AND completed_at IS NOT NULL
            """,
            recovery_user_id,
        )
        audit = await conn.fetchrow(
            """
            SELECT kind, actor_id, payload->>'user_id' AS user_id
              FROM events
             WHERE kind = 'auth.recovery_admin_retired'
            """
        )
        audit_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE kind = 'auth.recovery_admin_retired'"
        )
        actor_token_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM tokens WHERE id = $1 AND user_id = $2)",
            actor_token_id,
            actor_user_id,
        )

    assert state["password_hash"].startswith("!retired-recovery-admin:")
    assert state["account_status"] == "suspended"
    assert state["is_admin"] is False
    assert state["is_recovery_admin"] is False
    assert state["account_kind"] == "human"
    assert state["auth_provider"] == "local"
    assert state["tokens_revoked_before"] > before
    assert state["token_count"] == 0
    assert state["identity_count"] == 0
    assert cleanup_count == len(target_token_ids)
    assert dict(audit) == {
        "kind": "auth.recovery_admin_retired",
        "actor_id": str(actor_user_id),
        "user_id": str(recovery_user_id),
    }
    assert audit_count == 1
    assert actor_token_exists is True


async def test_retirement_denial_survives_failed_role_cleanup_and_retry_finishes(
    services,
    monkeypatch,
):
    pool, role_sync, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import CredentialCleanupIncompleteError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    recovery_user_id = uuid.UUID(provisioned["user_id"])
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    target_token_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tokens (id, user_id, name, token_hash, token_prefix, key_class)
            VALUES ($1, $2, 'legacy', $3, 'akb_recover', 'pat')
            """,
            target_token_id,
            recovery_user_id,
            uuid.uuid4().hex,
        )

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    role_sync.fail_token = target_token_id
    with pytest.raises(CredentialCleanupIncompleteError):
        await service.retire_local_recovery_admin(
            expected_username="local-recovery-admin",
            expected_email="local-recovery-admin@example.com",
            actor_user_id=str(actor_user_id),
            actor_token_id=str(actor_token_id),
        )

    async with pool.acquire() as conn:
        denied = await conn.fetchrow(
            """
            SELECT account_status, is_admin, is_recovery_admin,
                   EXISTS (SELECT 1 FROM tokens WHERE id = $2) AS token_exists,
                   EXISTS (
                       SELECT 1 FROM account_token_cleanup
                        WHERE token_id = $2 AND completed_at IS NULL
                   ) AS cleanup_pending
              FROM users WHERE id = $1
            """,
            recovery_user_id,
            target_token_id,
        )
    assert dict(denied) == {
        "account_status": "suspended",
        "is_admin": False,
        "is_recovery_admin": False,
        "token_exists": False,
        "cleanup_pending": True,
    }

    role_sync.fail_token = None
    retried = await service.retire_local_recovery_admin(
        expected_username="local-recovery-admin",
        expected_email="local-recovery-admin@example.com",
        actor_user_id=str(actor_user_id),
        actor_token_id=str(actor_token_id),
    )

    assert retried["user_id"] == str(recovery_user_id)
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT completed_at IS NOT NULL FROM account_token_cleanup WHERE token_id = $1",
            target_token_id,
        ) is True
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE kind = 'auth.recovery_admin_retired'"
        ) == 1


@pytest.mark.parametrize("creator", ["on_create", "sync_user", "reconcile"])
async def test_retirement_serializes_delayed_scoped_token_role_creation(
    services,
    monkeypatch,
    creator,
):
    pool, _, service, account_service, _, _ = services
    from app.config import settings
    from app.models.vault_scope import VaultScope
    from app.services.role_sync import ReconcileReport, RoleSync, token_role_name

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    recovery_user_id = uuid.UUID(provisioned["user_id"])
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    target_token_id = uuid.uuid4()
    scope = VaultScope(prefixes=("managed-",), extra_vaults=frozenset())
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tokens (
                id, user_id, name, token_hash, token_prefix, scopes,
                vault_scope, key_class
            ) VALUES ($1, $2, 'delayed-scoped', $3, 'akb_delayed_',
                      ARRAY['read', 'write'], $4::jsonb, 'pat')
            """,
            target_token_id,
            recovery_user_id,
            uuid.uuid4().hex,
            '{"prefixes":["managed-"],"extra_vaults":[]}',
        )

    actual_role_sync = RoleSync(pool)
    monkeypatch.setattr(account_service, "get_role_sync", lambda: actual_role_sync)
    role_name = token_role_name(target_token_id)
    create_entered = asyncio.Event()
    release_create = asyncio.Event()
    original_create = actual_role_sync._create_role_if_missing

    async def delayed_create(conn, role):
        if role == role_name:
            create_entered.set()
            await release_create.wait()
        await original_create(conn, role)

    monkeypatch.setattr(actual_role_sync, "_create_role_if_missing", delayed_create)
    async def create_role_path():
        if creator == "on_create":
            await actual_role_sync.on_token_create(
                target_token_id,
                recovery_user_id,
                scope,
            )
        elif creator == "sync_user":
            async with pool.acquire() as conn:
                await actual_role_sync._sync_user_scoped_tokens(conn, recovery_user_id)
        else:
            async with pool.acquire() as conn:
                await actual_role_sync._reconcile_token_roles(conn, ReconcileReport())

    create_task = asyncio.create_task(create_role_path())
    retire_task = None
    try:
        await asyncio.wait_for(create_entered.wait(), timeout=2)
        monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
        retire_task = asyncio.create_task(
            service.retire_local_recovery_admin(
                expected_username="local-recovery-admin",
                expected_email="local-recovery-admin@example.com",
                actor_user_id=str(actor_user_id),
                actor_token_id=str(actor_token_id),
            )
        )
        await asyncio.sleep(0.05)
        assert not retire_task.done()
        release_create.set()
        await asyncio.wait_for(asyncio.gather(create_task, retire_task), timeout=5)

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM tokens WHERE id = $1)",
                target_token_id,
            ) is False
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                role_name,
            ) is False
    finally:
        release_create.set()
        if not create_task.done():
            create_task.cancel()
        if retire_task is not None and not retire_task.done():
            retire_task.cancel()
        await asyncio.gather(
            create_task,
            *(tuple() if retire_task is None else (retire_task,)),
            return_exceptions=True,
        )
        async with pool.acquire() as conn:
            await actual_role_sync._drop_role_if_present(conn, role_name)


@pytest.mark.parametrize("target_by_id", [True, False], ids=["explicit-id", "email"])
async def test_retired_tombstone_rejects_external_identity_adoption(
    services,
    monkeypatch,
    target_by_id,
):
    pool, _, service, account_service, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminProtectedError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    await service.retire_local_recovery_admin(
        expected_username="local-recovery-admin",
        expected_email="local-recovery-admin@example.com",
        actor_user_id=str(actor_user_id),
        actor_token_id=str(actor_token_id),
    )

    with pytest.raises(RecoveryAdminProtectedError):
        await account_service.ensure_human_external_identity(
            issuer="https://identity.example.com/realms/example",
            subject=f"subject-{target_by_id}",
            email="local-recovery-admin@example.com",
            display_name="Retired recovery admin",
            actor_id=str(actor_user_id),
            existing_user_id=provisioned["user_id"] if target_by_id else None,
        )

    async with pool.acquire() as conn:
        state = await conn.fetchrow(
            """
            SELECT auth_provider, account_status, is_admin, is_recovery_admin,
                   (SELECT COUNT(*) FROM external_identities WHERE user_id = users.id)
                       AS identity_count
              FROM users WHERE id = $1
            """,
            uuid.UUID(provisioned["user_id"]),
        )
    assert dict(state) == {
        "auth_provider": "local",
        "account_status": "suspended",
        "is_admin": False,
        "is_recovery_admin": False,
        "identity_count": 0,
    }


async def test_token_role_reconcile_savepoint_isolates_one_ddl_failure(
    services,
    monkeypatch,
):
    pool, _, _, _, _, _ = services
    from app.services.role_sync import ReconcileReport, RoleSync, token_role_name

    user_id = uuid.uuid4()
    failed_token_id = uuid.uuid4()
    healthy_token_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash)
            VALUES ($1, $2, $3, '!hash!')
            """,
            user_id,
            f"reconcile-{user_id.hex}",
            f"reconcile-{user_id.hex}@example.test",
        )
        await conn.executemany(
            """
            INSERT INTO tokens (
                id, user_id, name, token_hash, token_prefix, vault_scope
            ) VALUES ($1, $2, $3, $4, 'akb_scope_', $5::jsonb)
            """,
            [
                (
                    failed_token_id,
                    user_id,
                    "failed-role",
                    uuid.uuid4().hex,
                    '{"prefixes":["managed-"],"extra_vaults":[]}',
                ),
                (
                    healthy_token_id,
                    user_id,
                    "healthy-role",
                    uuid.uuid4().hex,
                    '{"prefixes":["managed-"],"extra_vaults":[]}',
                ),
            ],
        )

    role_sync = RoleSync(pool)
    failed_role = token_role_name(failed_token_id)
    healthy_role = token_role_name(healthy_token_id)
    original_create = role_sync._create_role_if_missing

    async def fail_one_create(conn, role):
        if role == failed_role:
            await conn.execute("SELECT 1 / 0")
            return
        await original_create(conn, role)

    monkeypatch.setattr(role_sync, "_create_role_if_missing", fail_one_create)
    report = ReconcileReport()
    try:
        async with pool.acquire() as conn:
            await role_sync._reconcile_token_roles(conn, report)
            roles = {
                row["rolname"]
                for row in await conn.fetch(
                    "SELECT rolname FROM pg_roles WHERE rolname = ANY($1::text[])",
                    [failed_role, healthy_role],
                )
            }
        assert roles == {healthy_role}
        assert report.token_roles_created == 1
        assert len(report.errors) == 1
        assert failed_role in report.errors[0]
    finally:
        async with pool.acquire() as conn:
            await role_sync._drop_role_if_present(conn, failed_role)
            await role_sync._drop_role_if_present(conn, healthy_role)


async def test_retirement_mismatch_external_binding_and_collisions_fail_closed(
    services,
    monkeypatch,
):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminRetirementConflictError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    recovery_user_id = uuid.UUID(provisioned["user_id"])
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)

    with pytest.raises(RecoveryAdminRetirementConflictError):
        await service.retire_local_recovery_admin(
            expected_username="local-recovery-admin",
            expected_email="wrong@example.com",
            actor_user_id=str(actor_user_id),
            actor_token_id=str(actor_token_id),
        )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO external_identities (user_id, issuer, subject, email_snapshot)
            VALUES ($1, 'https://identity.example.com/realms/example', $2, $3)
            """,
            recovery_user_id,
            f"subject-{uuid.uuid4()}",
            "local-recovery-admin@example.com",
        )

    with pytest.raises(RecoveryAdminRetirementConflictError):
        await service.retire_local_recovery_admin(
            expected_username="local-recovery-admin",
            expected_email="local-recovery-admin@example.com",
            actor_user_id=str(actor_user_id),
            actor_token_id=str(actor_token_id),
        )

    async with pool.acquire() as conn:
        state = await conn.fetchrow(
            "SELECT account_status, is_admin, is_recovery_admin FROM users WHERE id = $1",
            recovery_user_id,
        )
    assert dict(state) == {
        "account_status": "active",
        "is_admin": True,
        "is_recovery_admin": True,
    }


async def test_retirement_revalidates_service_actor_and_split_collision(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import (
        RecoveryAdminRetirementAuthorizationError,
        RecoveryAdminRetirementConflictError,
    )

    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    human_actor_id = uuid.uuid4()
    human_actor_token_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, is_admin,
                auth_provider, account_status, account_kind
            ) VALUES ($1, 'human-admin', 'human-admin@example.com', '!hash!', true,
                      'local', 'active', 'human')
            """,
            human_actor_id,
        )
        await conn.execute(
            """
            INSERT INTO tokens (id, user_id, name, token_hash, token_prefix, key_class)
            VALUES ($1, $2, 'human-admin', $3, 'akb_human_', 'pat')
            """,
            human_actor_token_id,
            human_actor_id,
            uuid.uuid4().hex,
        )
        await conn.execute(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES ('split-username', 'other@example.com', '!hash!'),
                   ('other-username', 'split-email@example.com', '!hash!')
            """
        )

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    with pytest.raises(RecoveryAdminRetirementAuthorizationError):
        await service.retire_local_recovery_admin(
            expected_username="split-username",
            expected_email="split-email@example.com",
            actor_user_id=str(human_actor_id),
            actor_token_id=str(human_actor_token_id),
        )

    with pytest.raises(RecoveryAdminRetirementConflictError):
        await service.retire_local_recovery_admin(
            expected_username="split-username",
            expected_email="split-email@example.com",
            actor_user_id=str(actor_user_id),
            actor_token_id=str(actor_token_id),
        )


async def test_migration_upgrades_old_schema_and_is_idempotent(services):
    pool, _, _, _, _, _ = services
    from app.db.postgres import _load_migration

    async with pool.acquire() as conn:
        await conn.execute("DROP INDEX users_one_recovery_admin")
        await conn.execute("ALTER TABLE users DROP CONSTRAINT users_recovery_admin_requires_admin")
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


async def test_sso_bootstrap_retirement_receipt_migrates_and_is_monotonic(
    services,
    monkeypatch,
):
    pool, _, _, _, _, _ = services
    from app.db.postgres import _load_migration
    from app.services import standalone_sso_receipt as receipt_service
    from app.services.standalone_sso_bootstrap import (
        STANDALONE_SSO_RECEIPT_PROFILE,
        StandaloneSSOBootstrapError,
        StandaloneSSORetirementReceipt,
    )

    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE standalone_sso_bootstrap_retirements")
        migration = _load_migration("073_sso_bootstrap_receipt.py")
        callback_migration = _load_migration("075_sso_callback_receipt.py")
        assert migration is not None
        assert callback_migration is not None
        await migration.migrate(conn)
        await migration.migrate(conn)
        await callback_migration.migrate(conn)
        await callback_migration.migrate(conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(receipt_service, "get_pool", _get_pool)
    expected = StandaloneSSORetirementReceipt(
        profile=STANDALONE_SSO_RECEIPT_PROFILE,
        issuer="https://auth.akb.example.com/realms/akb",
        realm_id="akb-realm-id",
        bootstrap_client_id="akb-bootstrap-temporary",
        management_client_uuid="management-client-uuid",
        admin_client_uuid="admin-client-uuid",
        api_client_uuid="api-client-uuid",
        product_admin_subject="00000000-0000-4000-8000-000000000001",
        akb_user_id="11111111-1111-4111-8111-111111111111",
        backchannel_logout_uri=("https://akb.example.com/api/v1/auth/keycloak/backchannel-logout"),
    )

    assert await receipt_service.load_standalone_sso_retirement_receipt() is None
    await receipt_service.record_standalone_sso_retirement_receipt(expected)
    await receipt_service.record_standalone_sso_retirement_receipt(expected)
    assert await receipt_service.load_standalone_sso_retirement_receipt() == expected

    migrated = replace(
        expected,
        bootstrap_client_id="akb-bootstrap-upgrade-v2",
        backchannel_logout_uri=("http://backend:8000/api/v1/auth/keycloak/backchannel-logout"),
    )
    await receipt_service.record_standalone_sso_retirement_receipt(
        migrated,
        previous_receipt=expected,
    )
    assert await receipt_service.load_standalone_sso_retirement_receipt() == migrated

    with pytest.raises(StandaloneSSOBootstrapError):
        await receipt_service.record_standalone_sso_retirement_receipt(
            replace(migrated, issuer="https://other.example.com/realms/akb")
        )

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM standalone_sso_bootstrap_retirements") == 1
