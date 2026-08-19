"""PostgreSQL contracts for the non-human service-authority binding.

The point of these is the separation: the machine principal must be a real,
durable AKB account and must be reachable by nothing on the human path.
"""

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

ISSUER = "https://auth-workspace.example.com/realms/akb"
CLIENT_ID = "akb-sso-manager"
SUBJECT = "a9cf6dcd-46ec-42b6-94f4-b6f0312ec15f"


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

    database = f"akb_service_authority_{uuid.uuid4().hex[:12]}"
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

    async def on_user_create(self, user_id):
        self.created.append(uuid.UUID(str(user_id)))


@pytest.fixture
async def services(monkeypatch):
    async with _fresh_database() as pool:
        from app.services import account_service, auth_service, service_authority_service

        role_sync = _RoleSync()

        async def _get_pool():
            return pool

        for module in (account_service, auth_service, service_authority_service):
            monkeypatch.setattr(module, "get_pool", _get_pool)
            monkeypatch.setattr(module, "get_role_sync", lambda: role_sync)

        yield pool, role_sync, service_authority_service, account_service, auth_service


async def test_the_migration_reproduces_the_bundled_schema(services):
    """081 on a database created before it must land the same shape init.sql has."""
    pool, *_ = services
    from app.db.postgres import _load_migration

    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE service_identities")
        migration = _load_migration("081_service_identities.py")
        assert migration is not None
        await migration.migrate(conn)

        columns = {
            row["column_name"]: row["is_nullable"]
            for row in await conn.fetch(
                """
                SELECT column_name, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'service_identities'
                """
            )
        }
        assert columns == {
            "id": "NO",
            "user_id": "NO",
            "issuer": "NO",
            "client_id": "NO",
            "subject": "NO",
            "created_at": "NO",
            "last_seen_at": "NO",
        }
        # Applying it twice is a no-op, the way a restart applies it.
        await migration.migrate(conn)


async def test_first_use_creates_exactly_a_non_human_administrator(services):
    pool, role_sync, service_authority_service, _, _ = services

    resolved = await service_authority_service.resolve_service_authority(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        subject=SUBJECT,
    )

    assert resolved["newly_bound"] is True
    assert resolved["is_admin"] is True
    assert resolved["username"] == f"service-{CLIENT_ID}"
    assert role_sync.created == [uuid.UUID(str(resolved["user_id"]))]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT account_kind, auth_provider, account_status, is_admin,
                   is_recovery_admin, credential_change_required, password_hash
              FROM users WHERE id = $1
            """,
            resolved["user_id"],
        )
        assert row["account_kind"] == "service"
        assert row["auth_provider"] == "service"
        assert row["account_status"] == "active"
        assert row["is_admin"] is True
        assert row["is_recovery_admin"] is False
        assert row["credential_change_required"] is False
        # No usable local credential exists for this account.
        assert row["password_hash"].startswith("!")

        # The machine is not in the human population.
        assert await conn.fetchval("SELECT COUNT(*) FROM external_identities") == 0
        binding = await conn.fetchrow("SELECT * FROM service_identities")
        assert binding["issuer"] == ISSUER
        assert binding["client_id"] == CLIENT_ID
        assert binding["subject"] == SUBJECT
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE kind = 'auth.service_authority_bound'"
        ) == 1


async def test_repeat_use_is_idempotent_and_concurrent_first_use_converges(services):
    pool, role_sync, service_authority_service, _, _ = services

    first = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    again = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    assert again["user_id"] == first["user_id"]
    assert again["newly_bound"] is False

    concurrent = await asyncio.gather(
        *(
            service_authority_service.resolve_service_authority(
                issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
            )
            for _ in range(6)
        )
    )
    assert {str(result["user_id"]) for result in concurrent} == {str(first["user_id"])}
    assert role_sync.created == [uuid.UUID(str(first["user_id"]))]

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users") == 1
        assert await conn.fetchval("SELECT COUNT(*) FROM service_identities") == 1


async def test_a_recreated_client_refreshes_the_binding_instead_of_adding_an_administrator(
    services,
):
    """Deleting and recreating the client in the realm yields a new
    service-account user. That is the same authority, not a second one."""
    pool, _, service_authority_service, _, _ = services

    first = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    recreated_subject = "0f0f0f0f-1111-4222-8333-444444444444"
    after = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=recreated_subject
    )

    assert after["user_id"] == first["user_id"]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_admin") == 1
        assert await conn.fetchval("SELECT subject FROM service_identities") == recreated_subject


async def test_a_second_authority_is_a_second_account_and_never_shares_one(services):
    pool, _, service_authority_service, _, _ = services

    first = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    other = await service_authority_service.resolve_service_authority(
        issuer=ISSUER,
        client_id="akb-other-authority",
        subject="11111111-2222-4333-8444-555555555555",
    )
    assert other["user_id"] != first["user_id"]

    async with pool.acquire() as conn:
        # And the same realm subject can never be bound twice. A third,
        # otherwise unbound account isolates this to the subject constraint —
        # binding it to `first`'s account would collide on user_id instead.
        third = await conn.fetchval(
            """
            INSERT INTO users (username, email, password_hash, auth_provider, account_kind)
            VALUES ('service-akb-third-authority', 'third@service.invalid', '!x!', 'service', 'service')
            RETURNING id
            """
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO service_identities (user_id, issuer, client_id, subject)
                VALUES ($1, $2, $3, $4)
                """,
                third,
                ISSUER,
                "akb-third-authority",
                SUBJECT,
            )


async def test_suspension_revokes_the_authority(services):
    from app.exceptions import AccountSuspendedError

    pool, _, service_authority_service, account_service, _ = services

    resolved = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    await account_service.suspend_user(str(resolved["user_id"]), actor_id=str(resolved["user_id"]))

    with pytest.raises(AccountSuspendedError):
        await service_authority_service.resolve_service_authority(
            issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
        )


async def test_an_operator_demotion_is_respected_rather_than_silently_re_granted(services):
    pool, _, service_authority_service, _, _ = services

    resolved = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_admin = false WHERE id = $1", resolved["user_id"])

    after = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    assert after["user_id"] == resolved["user_id"]
    assert after["is_admin"] is False


async def test_a_bound_account_that_stops_being_a_service_principal_is_refused(services):
    from app.exceptions import ExternalIdentityConflictError

    pool, _, service_authority_service, _, _ = services

    resolved = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO external_identities (user_id, issuer, subject)
            VALUES ($1, $2, $3)
            """,
            resolved["user_id"],
            ISSUER,
            "some-human-subject",
        )

    with pytest.raises(ExternalIdentityConflictError):
        await service_authority_service.resolve_service_authority(
            issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
        )


async def test_a_username_collision_is_refused_rather_than_adopted(services):
    from app.exceptions import ExternalIdentityConflictError

    pool, _, service_authority_service, _, _ = services

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (username, email, password_hash, auth_provider, account_kind)
            VALUES ($1, $2, 'x', 'local', 'human')
            """,
            f"service-{CLIENT_ID}",
            "someone@example.com",
        )

    with pytest.raises(ExternalIdentityConflictError):
        await service_authority_service.resolve_service_authority(
            issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM service_identities") == 0


async def test_the_human_path_cannot_see_the_bound_machine_identity(services):
    """The measured token's subject stays unenrollable as a person: under
    invite-only there is no prebound human identity for it, and the machine's
    own binding is invisible to that lookup."""
    from app.config import settings
    from app.exceptions import MembershipRequiredError

    _, _, service_authority_service, _, auth_service = services

    resolved = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    assert resolved["user_id"] is not None

    settings_backup = settings.keycloak_enrollment_mode
    object.__setattr__(settings, "keycloak_enrollment_mode", "invite_only")
    try:
        async with (await auth_service.get_pool()).acquire() as conn:
            assert await auth_service._bound_external_user(conn, ISSUER, SUBJECT) is None
        with pytest.raises(MembershipRequiredError):
            await auth_service._resolve_or_provision_keycloak_user(
                {
                    "iss": ISSUER,
                    "sub": SUBJECT,
                    "preferred_username": f"service-account-{CLIENT_ID}",
                }
            )
    finally:
        object.__setattr__(settings, "keycloak_enrollment_mode", settings_backup)


async def test_the_bound_machine_is_not_an_independent_service_administrator(services):
    """The recovery-administrator retirement authority requires a service PAT
    that this principal does not have. Verified rather than assumed."""
    pool, _, service_authority_service, _, _ = services
    from app.exceptions import RecoveryAdminProtectedError
    from app.services import recovery_admin_service

    resolved = await service_authority_service.resolve_service_authority(
        issuer=ISSUER, client_id=CLIENT_ID, subject=SUBJECT
    )
    async with pool.acquire() as conn:
        with pytest.raises(RecoveryAdminProtectedError):
            await recovery_admin_service._require_independent_service_admin(
                conn,
                actor_user_id=str(resolved["user_id"]),
                actor_token_id=str(uuid.uuid4()),
                refusal=RecoveryAdminProtectedError,
            )
