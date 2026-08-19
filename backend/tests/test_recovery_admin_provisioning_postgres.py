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
            password_service,
            recovery_admin_service,
        )

        role_sync = _RoleSync()

        async def _get_pool():
            return pool

        for module in (
            access_service,
            account_service,
            auth_service,
            password_service,
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


async def test_local_provisioning_rejects_an_unrecognised_credential_marker(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminConflictError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    # A designated row whose credential column holds something this service
    # never wrote. Converging on it would adopt an account of unknown
    # provenance as the one that can recover the installation.
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (username, email, password_hash, is_admin,
                               is_recovery_admin, auth_provider,
                               account_status, account_kind)
            VALUES ('recovery-admin', 'recovery-admin@example.com',
                    '!some-other-marker!', true, true, 'local', 'active', 'human')
            """
        )

    with pytest.raises(RecoveryAdminConflictError):
        await service.provision_local_recovery_admin(
            username="recovery-admin",
            email="recovery-admin@example.com",
            password=_LOCAL_PASSWORD,
        )


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


async def test_sso_prebinding_rejects_oversized_subject_without_mutation(
    services,
    monkeypatch,
):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import ValidationError
    from app.services.external_identity_contract import OIDC_SUBJECT_MAX_LENGTH

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)

    with pytest.raises(ValidationError):
        await service.provision_sso_recovery_admin(
            username="sso-recovery-admin",
            email="sso-recovery-admin@example.com",
            issuer=settings.keycloak_issuer,
            subject="s" * (OIDC_SUBJECT_MAX_LENGTH + 1),
        )

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users") == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM external_identities") == 0


async def test_sso_prebinding_overlaps_local_recovery_until_exact_retirement(
    services,
    monkeypatch,
):
    pool, _, service, _, _, _ = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    local = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    sso = await service.provision_sso_recovery_admin(
        username="admin",
        email="admin@example.com",
        issuer=settings.keycloak_issuer,
        subject="stable-admin-subject",
    )

    async with pool.acquire() as conn:
        providers = await conn.fetch(
            "SELECT auth_provider FROM users WHERE is_recovery_admin ORDER BY auth_provider"
        )
    assert [row["auth_provider"] for row in providers] == ["keycloak", "local"]

    retired = await service.retire_local_recovery_admin(
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

    assert retired["user_id"] == local["user_id"]
    assert repeated == retired
    async with pool.acquire() as conn:
        active_recovery = await conn.fetchrow(
            "SELECT id, auth_provider FROM users WHERE is_recovery_admin"
        )
        local_state = await conn.fetchrow(
            "SELECT is_admin, is_recovery_admin, account_status FROM users WHERE id = $1",
            uuid.UUID(local["user_id"]),
        )
    assert dict(active_recovery) == {
        "id": uuid.UUID(sso["user_id"]),
        "auth_provider": "keycloak",
    }
    assert dict(local_state) == {
        "is_admin": False,
        "is_recovery_admin": False,
        "account_status": "suspended",
    }


async def test_retirement_rejects_sso_successor_bound_to_stale_issuer(
    services,
    monkeypatch,
):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminRetirementConflictError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    local = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    stale_issuer = settings.keycloak_issuer + "-stale"
    await service.provision_sso_recovery_admin(
        username="admin",
        email="admin@example.com",
        issuer=stale_issuer,
        subject="stale-admin-subject",
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
            "SELECT is_admin, is_recovery_admin, account_status FROM users WHERE id = $1",
            uuid.UUID(local["user_id"]),
        )
    assert dict(state) == {
        "is_admin": True,
        "is_recovery_admin": True,
        "account_status": "active",
    }


async def test_retirement_rejects_legacy_oversized_sso_subject(
    services,
    monkeypatch,
):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminRetirementConflictError
    from app.services.external_identity_contract import OIDC_SUBJECT_MAX_LENGTH

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    local = await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    successor = await _create_sso_recovery_successor(service)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE external_identities SET subject = $2 WHERE user_id = $1",
            uuid.UUID(successor["user_id"]),
            "s" * (OIDC_SUBJECT_MAX_LENGTH + 1),
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
            "SELECT is_admin, is_recovery_admin, account_status FROM users WHERE id = $1",
            uuid.UUID(local["user_id"]),
        )
    assert dict(state) == {
        "is_admin": True,
        "is_recovery_admin": True,
        "account_status": "active",
    }


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


async def _create_sso_recovery_successor(service) -> dict:
    from app.config import settings

    return await service.provision_sso_recovery_admin(
        username="sso-recovery-admin",
        email="sso-recovery-admin@example.com",
        issuer=settings.keycloak_issuer,
        subject="stable-sso-recovery-subject",
    )


async def test_retirement_locks_external_identity_before_successor_user(
    services,
    monkeypatch,
):
    pool, _, service, _, _, _ = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    await service.provision_local_recovery_admin(
        username="local-recovery-admin",
        email="local-recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_user_id, actor_token_id = await _create_retirement_actor(pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    successor = await _create_sso_recovery_successor(service)
    successor_id = uuid.UUID(successor["user_id"])

    blocker = await pool.acquire()
    blocker_tx = blocker.transaction()
    await blocker_tx.start()
    blocker_tx_finished = False
    retire_task: asyncio.Task | None = None
    try:
        await blocker.execute(
            """
            UPDATE external_identities
               SET last_seen_at = last_seen_at
             WHERE user_id = $1
            """,
            successor_id,
        )
        retire_task = asyncio.create_task(
            service.retire_local_recovery_admin(
                expected_username="local-recovery-admin",
                expected_email="local-recovery-admin@example.com",
                actor_user_id=str(actor_user_id),
                actor_token_id=str(actor_token_id),
            )
        )

        observed_identity_wait = False
        for _ in range(100):
            async with pool.acquire() as observer:
                observed_identity_wait = bool(
                    await observer.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                              FROM pg_stat_activity
                             WHERE datname = current_database()
                               AND pid <> pg_backend_pid()
                               AND query LIKE '%recovery-successor-authority-lock%'
                               AND wait_event_type = 'Lock'
                        )
                        """
                    )
                )
            if observed_identity_wait:
                break
            await asyncio.sleep(0.01)
        assert observed_identity_wait

        await blocker.execute(
            "UPDATE users SET updated_at = NOW() WHERE id = $1",
            successor_id,
        )
        await blocker_tx.commit()
        blocker_tx_finished = True
        await asyncio.wait_for(retire_task, timeout=5)
    finally:
        if not blocker_tx_finished:
            await blocker_tx.rollback()
        await pool.release(blocker)
        if retire_task is not None and not retire_task.done():
            retire_task.cancel()
            await asyncio.gather(retire_task, return_exceptions=True)

    async with pool.acquire() as conn:
        active = await conn.fetchrow(
            "SELECT id, auth_provider FROM users WHERE is_recovery_admin"
        )
    assert dict(active) == {"id": successor_id, "auth_provider": "keycloak"}


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
    await _create_sso_recovery_successor(service)
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
    await _create_sso_recovery_successor(service)
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

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    await _create_sso_recovery_successor(service)
    create_task = asyncio.create_task(create_role_path())
    retire_task = None
    try:
        await asyncio.wait_for(create_entered.wait(), timeout=2)
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
    await _create_sso_recovery_successor(service)
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
    await _create_sso_recovery_successor(service)

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
    await _create_sso_recovery_successor(service)
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
        await conn.execute("DROP INDEX users_one_recovery_admin_per_provider")
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


async def test_recovery_handover_migration_allows_one_recovery_per_provider(services):
    pool, _, _, _, _, _ = services
    from app.db.postgres import _load_migration

    async with pool.acquire() as conn:
        await conn.execute("DROP INDEX users_one_recovery_admin_per_provider")
        await conn.execute(
            "ALTER TABLE users DROP CONSTRAINT users_recovery_admin_provider_check"
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX users_one_recovery_admin
                ON users ((is_recovery_admin))
                WHERE is_recovery_admin
            """
        )
        await conn.execute(
            """
            INSERT INTO users (
                username, email, password_hash, is_admin, is_recovery_admin,
                auth_provider
            ) VALUES ('migration-local', 'migration-local@example.com',
                      '!local!', true, true, 'local')
            """
        )
        migration = _load_migration("078_recovery_admin_authority_handover.py")
        assert migration is not None
        await migration.migrate(conn)
        await migration.migrate(conn)

        await conn.execute(
            """
            INSERT INTO users (
                username, email, password_hash, is_admin, is_recovery_admin,
                auth_provider
            ) VALUES ('migration-sso', 'migration-sso@example.com',
                      '!sso!', true, true, 'keycloak')
            """
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO users (
                    username, email, password_hash, is_admin, is_recovery_admin,
                    auth_provider
                ) VALUES ('migration-local-two', 'migration-local-two@example.com',
                          '!local-two!', true, true, 'local')
                """
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO users (
                    username, email, password_hash, is_admin, is_recovery_admin,
                    auth_provider
                ) VALUES ('migration-foreign', 'migration-foreign@example.com',
                          '!foreign!', true, true, 'foreign')
                """
            )
        indexes = await conn.fetch(
            """
            SELECT indexname FROM pg_indexes
             WHERE schemaname = 'public' AND tablename = 'users'
               AND indexname LIKE 'users_one_recovery_admin%'
             ORDER BY indexname
            """
        )

    assert [row["indexname"] for row in indexes] == [
        "users_one_recovery_admin_per_provider"
    ]


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


async def test_credential_issue_replaces_the_credential_the_account_already_had(
    services,
    monkeypatch,
):
    """Rotation, not first issue: the old credential must stop working.

    The account is installed enterable, so this endpoint replaces a credential
    that already exists. It is the response to a credential that leaked or was
    lost, which is only true if the previous one dies with the rotation — and
    it rotates a credential currently in use, deliberately, because that is
    what a compromise response has to do.
    """
    pool, _, service, _, _, auth_service = services
    from app.config import settings
    from app.exceptions import AuthenticationError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_id, token_id = await _create_retirement_actor(pool)
    # The installed credential works before the rotation, and the session it
    # mints is live. That credential was delivered, so the session it mints
    # may do nothing but replace it — resolve it through the entry point that
    # serves the forced credential change, which is the one thing this account
    # can still reach.
    signed_in = await auth_service.login("recovery-admin", _LOCAL_PASSWORD)
    assert signed_in["user"]["id"] == provisioned["user_id"]
    held_session = f"Bearer {signed_in['token']}"
    assert (
        await auth_service.resolve_rest_credential_change_authorization(held_session)
        is not None
    )

    issued = await service.issue_recovery_admin_credential(
        expected_username="recovery-admin",
        expected_email="recovery-admin@example.com",
        method="recovery_admin_api",
        actor_user_id=str(actor_id),
        actor_token_id=str(token_id),
    )

    assert issued["user_id"] == provisioned["user_id"]
    assert issued["username"] == "recovery-admin"
    assert issued["auth_mode"] == "local"
    credential = issued["credential"]
    assert isinstance(credential, str) and credential

    async with pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    assert auth_service.verify_password(credential, stored)
    login = await auth_service.login("recovery-admin", credential)
    assert login["user"]["id"] == provisioned["user_id"]

    # And the credential it replaced no longer opens the account.
    assert not auth_service.verify_password(_LOCAL_PASSWORD, stored)
    with pytest.raises(AuthenticationError):
        await auth_service.login("recovery-admin", _LOCAL_PASSWORD)

    # Nor does the session that credential had already minted. Replacing the
    # credential without this would leave whoever held the old one signed in,
    # which is the case the rotation exists for. The delegate does it; assert
    # it here so the behaviour cannot be inherited silently.
    assert await auth_service.resolve_rest_user_authorization(held_session) is None


async def test_credential_issue_never_writes_the_value_into_any_audit_payload(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_id, token_id = await _create_retirement_actor(pool)

    issued = await service.issue_recovery_admin_credential(
        expected_username="recovery-admin",
        expected_email="recovery-admin@example.com",
        method="recovery_admin_api",
        actor_user_id=str(actor_id),
        actor_token_id=str(token_id),
    )

    async with pool.acquire() as conn:
        every_payload = await conn.fetch("SELECT kind, payload::text AS body FROM events")
        stored = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    assert every_payload
    for record in every_payload:
        assert issued["credential"] not in record["body"], record["kind"]
    # Not recoverable from the row either: only the bcrypt hash is stored.
    assert issued["credential"] not in stored

    issued_events = [
        record for record in every_payload
        if record["kind"] == "auth.recovery_admin_credential_issued"
    ]
    assert len(issued_events) == 1
    assert "recovery_admin_api" in issued_events[0]["body"]


async def test_credential_issue_requires_an_independent_service_administrator(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminCredentialAuthorizationError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    async with pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    # The designated recovery admin is not an independent authority over itself.
    async with pool.acquire() as conn:
        self_token = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO tokens (id, user_id, name, token_hash, token_prefix, scopes, key_class)
            VALUES ($1, $2, 'self', $3, 'akb_secret_', ARRAY['read','write','admin'], 'service')
            """,
            self_token,
            uuid.UUID(provisioned["user_id"]),
            uuid.uuid4().hex,
        )

    with pytest.raises(RecoveryAdminCredentialAuthorizationError):
        await service.issue_recovery_admin_credential(
            expected_username="recovery-admin",
            expected_email="recovery-admin@example.com",
            method="recovery_admin_api",
            actor_user_id=provisioned["user_id"],
            actor_token_id=str(self_token),
        )

    async with pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    assert after == before


async def test_credential_issue_refuses_an_inexact_expected_identity(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminCredentialConflictError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_id, token_id = await _create_retirement_actor(pool)
    async with pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )

    for username, email in (
        ("recovery-admin", "someone-else@example.com"),
        ("someone-else", "recovery-admin@example.com"),
    ):
        with pytest.raises(RecoveryAdminCredentialConflictError):
            await service.issue_recovery_admin_credential(
                expected_username=username,
                expected_email=email,
                method="recovery_admin_api",
                actor_user_id=str(actor_id),
                actor_token_id=str(token_id),
            )

    async with pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    assert after == before


async def test_credential_issue_refuses_a_retired_recovery_administrator(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminCredentialConflictError
    from app.services.account_markers import RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_id, token_id = await _create_retirement_actor(pool)
    # Exactly the durable state retirement leaves behind.
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
               SET is_recovery_admin = false, is_admin = false,
                   account_status = 'suspended', password_hash = $2
             WHERE id = $1
            """,
            uuid.UUID(provisioned["user_id"]),
            RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL,
        )

    with pytest.raises(RecoveryAdminCredentialConflictError):
        await service.issue_recovery_admin_credential(
            expected_username="recovery-admin",
            expected_email="recovery-admin@example.com",
            method="recovery_admin_api",
            actor_user_id=str(actor_id),
            actor_token_id=str(token_id),
        )

    async with pool.acquire() as conn:
        unchanged = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    assert unchanged == RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL


async def test_break_glass_cli_issue_carries_no_actor_and_is_audited_apart(services, monkeypatch):
    pool, _, service, _, _, auth_service = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )

    issued = await service.issue_recovery_admin_credential(
        expected_username="recovery-admin",
        expected_email="recovery-admin@example.com",
        method="recovery_admin_cli",
    )

    async with pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
        events = await conn.fetch(
            """
            SELECT kind, actor_id, payload::text AS body
              FROM events
             WHERE kind IN ('auth.recovery_admin_credential_issued', 'auth.password_reset')
             ORDER BY kind
            """
        )
    assert auth_service.verify_password(issued["credential"], stored)
    by_type = {record["kind"]: record for record in events}
    # Shell access is the authority, so there is no authenticated principal.
    assert by_type["auth.recovery_admin_credential_issued"]["actor_id"] is None
    assert "recovery_admin_cli" in by_type["auth.recovery_admin_credential_issued"]["body"]
    # The delegated local reset stays visible and carries the same discriminator.
    assert "recovery_admin_cli" in by_type["auth.password_reset"]["body"]


async def test_break_glass_cli_method_refuses_a_supplied_actor(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminCredentialAuthorizationError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_id, token_id = await _create_retirement_actor(pool)

    # The break-glass path has no authenticated principal by construction.
    # Accepting one would let an API caller borrow the unauthenticated route.
    with pytest.raises(RecoveryAdminCredentialAuthorizationError):
        await service.issue_recovery_admin_credential(
            expected_username="recovery-admin",
            expected_email="recovery-admin@example.com",
            method="recovery_admin_cli",
            actor_user_id=str(actor_id),
            actor_token_id=str(token_id),
        )


async def test_credential_issue_advances_the_session_revocation_cutoff(services, monkeypatch):
    pool, _, service, _, _, auth_service = services
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_id, token_id = await _create_retirement_actor(pool)
    async with pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT tokens_revoked_before FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )

    await service.issue_recovery_admin_credential(
        expected_username="recovery-admin",
        expected_email="recovery-admin@example.com",
        method="recovery_admin_api",
        actor_user_id=str(actor_id),
        actor_token_id=str(token_id),
    )

    async with pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT tokens_revoked_before FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    # The mechanism behind the rejection asserted end-to-end in
    # test_credential_issue_replaces_the_credential_the_account_already_had.
    # Kept separately because a cutoff that stops advancing is a different
    # failure from a resolver that stops accepting anything.
    assert after > before


async def test_sso_credential_issue_fails_closed_without_a_minting_authority(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminCredentialUnavailableError

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    provisioned = await _create_sso_recovery_successor(service)
    actor_id, token_id = await _create_retirement_actor(pool)

    # A running AKB holds no authority that can mint a Keycloak credential:
    # the management client has no manage-users, and the bootstrap client is
    # deleted and proven dead before the install writes its receipt.
    with pytest.raises(RecoveryAdminCredentialUnavailableError) as captured:
        await service.issue_recovery_admin_credential(
            expected_username="sso-recovery-admin",
            expected_email="sso-recovery-admin@example.com",
            method="recovery_admin_api",
            actor_user_id=str(actor_id),
            actor_token_id=str(token_id),
        )

    # The code and status are what an API caller sees, so pin them rather than
    # only the class: renaming either is a contract change, not a refactor.
    assert captured.value.code == "recovery_admin_credential_rotation_unavailable"
    assert captured.value.status_code == 503

    from app.services.recovery_admin_service import _SSO_PASSWORD_SENTINEL

    async with pool.acquire() as conn:
        unchanged = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
        issued = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE kind = 'auth.recovery_admin_credential_issued'"
        )
    assert unchanged == _SSO_PASSWORD_SENTINEL
    assert issued == 0


async def test_sso_credential_issue_still_refuses_an_unauthorized_caller_first(services, monkeypatch):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminCredentialAuthorizationError

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    await _create_sso_recovery_successor(service)

    # Authorization is decided before capability, so an unauthorized caller
    # never learns which modes can mint.
    with pytest.raises(RecoveryAdminCredentialAuthorizationError):
        await service.issue_recovery_admin_credential(
            expected_username="sso-recovery-admin",
            expected_email="sso-recovery-admin@example.com",
            method="recovery_admin_api",
            actor_user_id=str(uuid.uuid4()),
            actor_token_id=str(uuid.uuid4()),
        )


@pytest.mark.parametrize(
    "column, value",
    [
        # Still the designated administrator, but not in a state we may issue
        # for. Retirement clears `is_recovery_admin`, so these are the states
        # the designated-row lookup itself cannot filter out.
        ("account_status", "suspended"),
        ("account_kind", "service"),
        ("password_hash", "!some-other-marker!"),
    ],
)
async def test_credential_issue_refuses_a_designated_row_in_a_bad_state(
    services,
    monkeypatch,
    column,
    value,
):
    pool, _, service, _, _, _ = services
    from app.config import settings
    from app.exceptions import RecoveryAdminCredentialConflictError

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    provisioned = await service.provision_local_recovery_admin(
        username="recovery-admin",
        email="recovery-admin@example.com",
        password=_LOCAL_PASSWORD,
    )
    actor_id, token_id = await _create_retirement_actor(pool)
    async with pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE users SET {column} = $2 WHERE id = $1",  # noqa: S608 - fixed test vocabulary
            uuid.UUID(provisioned["user_id"]),
            value,
        )
        assert await conn.fetchval(
            "SELECT is_recovery_admin FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )

    with pytest.raises(RecoveryAdminCredentialConflictError):
        await service.issue_recovery_admin_credential(
            expected_username="recovery-admin",
            expected_email="recovery-admin@example.com",
            method="recovery_admin_api",
            actor_user_id=str(actor_id),
            actor_token_id=str(token_id),
        )

    async with pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(provisioned["user_id"]),
        )
        issued = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE kind = 'auth.recovery_admin_credential_issued'"
        )
    assert after == (value if column == "password_hash" else before)
    assert issued == 0
