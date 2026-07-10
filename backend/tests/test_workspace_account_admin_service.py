"""Administrative workspace-account projection and revocation tests."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_ISSUER = "https://id.example.com/realms/akb"


class RecordingRoleSync:
    def __init__(self):
        self.created_users: list[uuid.UUID] = []
        self.revoked_tokens: list[uuid.UUID] = []
        self.fail_token: uuid.UUID | None = None

    async def on_user_create(self, user_id):
        self.created_users.append(uuid.UUID(str(user_id)))

    async def revoke_token_role_strict(self, token_id):
        token_uuid = uuid.UUID(str(token_id))
        if token_uuid == self.fail_token:
            raise RuntimeError("role cleanup failed")
        self.revoked_tokens.append(token_uuid)


@pytest.fixture
async def services(monkeypatch):
    try:
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    from app.db.postgres import _load_migration
    from app.services import account_service, auth_service, password_service

    migration = _load_migration("043_workspace_account_governance.py")
    assert migration is not None
    async with pool.acquire() as conn:
        await migration.migrate(conn=conn)

    role_sync = RecordingRoleSync()

    async def _get_pool():
        return pool

    monkeypatch.setattr(account_service, "get_pool", _get_pool)
    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(password_service, "get_pool", _get_pool)
    monkeypatch.setattr(account_service, "get_role_sync", lambda: role_sync)
    yield pool, role_sync, account_service

    async with pool.acquire() as conn:
        user_ids = await conn.fetch(
            "SELECT id FROM users WHERE username LIKE 'governance-%'"
        )
        ids = [str(row["id"]) for row in user_ids]
        await conn.execute("DELETE FROM users WHERE username LIKE 'governance-%'")
        if ids:
            await conn.execute(
                "DELETE FROM events WHERE payload->>'user_id' = ANY($1::text[])",
                ids,
            )
    await pool.close()


async def test_ensure_human_binding_is_idempotent_and_never_bootstrap_admin(services):
    pool, role_sync, service = services
    subject = f"human-{uuid.uuid4().hex}"
    email = f"governance-human-{uuid.uuid4().hex[:10]}@example.com"

    first = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=email,
        display_name="First name",
        actor_id="platform-service",
    )
    second = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=email,
        display_name="Updated name",
        actor_id="platform-service",
    )
    third = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=email,
        display_name=None,
        actor_id="platform-service",
    )

    assert first["user_id"] == second["user_id"]
    assert second["user_id"] == third["user_id"]
    assert first["is_admin"] is False
    assert second["display_name"] == "Updated name"
    assert third["display_name"] == "Updated name"
    assert role_sync.created_users == [uuid.UUID(first["user_id"])]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.auth_provider, u.account_status, u.account_kind,
                   e.issuer, e.subject
              FROM users u
              JOIN external_identities e ON e.user_id = u.id
             WHERE u.id = $1
            """,
            uuid.UUID(first["user_id"]),
        )
    assert dict(row) == {
        "auth_provider": "keycloak",
        "account_status": "active",
        "account_kind": "human",
        "issuer": _ISSUER,
        "subject": subject,
    }


async def test_concurrent_human_ensure_converges_to_one_user(services):
    pool, _, service = services
    subject = f"concurrent-human-{uuid.uuid4().hex}"
    email = f"governance-concurrent-human-{uuid.uuid4().hex[:10]}@example.com"

    results = await asyncio.gather(
        *[
            service.ensure_human_external_identity(
                issuer=_ISSUER,
                subject=subject,
                email=email,
                display_name="Concurrent member",
                actor_id="platform-service",
            )
            for _ in range(3)
        ]
    )

    assert len({result["user_id"] for result in results}) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM external_identities WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            subject,
        ) == 1


async def test_explicit_user_id_allows_reviewed_issuer_migration(services):
    pool, _, service = services
    email = f"governance-issuer-migration-{uuid.uuid4().hex[:10]}@example.com"
    first = await service.ensure_human_external_identity(
        issuer="https://old-id.example.com/realms/akb",
        subject="stable-old-subject",
        email=email,
        display_name="Migrating member",
        actor_id="platform-service",
    )

    migrated = await service.ensure_human_external_identity(
        issuer="https://new-id.example.com/realms/akb",
        subject="stable-new-subject",
        email=email,
        display_name="Migrating member",
        actor_id="platform-service",
        existing_user_id=first["user_id"],
    )

    assert migrated["user_id"] == first["user_id"]
    async with pool.acquire() as conn:
        bindings = await conn.fetchval(
            "SELECT COUNT(*) FROM external_identities WHERE user_id = $1",
            uuid.UUID(first["user_id"]),
        )
    assert bindings == 2


async def test_ensure_service_user_is_noninteractive_idempotent_and_non_admin(
    services,
    monkeypatch,
):
    pool, role_sync, service = services
    username = f"governance-runtime-{uuid.uuid4().hex[:10]}"
    email = f"{username}@service.akb.invalid"

    first = await service.ensure_service_user(
        username=username,
        email=email,
        display_name="Pipeline runtime",
        actor_id="platform-service",
    )
    second = await service.ensure_service_user(
        username=username,
        email=email,
        display_name="Pipeline runtime renamed",
        actor_id="platform-service",
    )
    third = await service.ensure_service_user(
        username=username,
        email=email,
        display_name=None,
        actor_id="platform-service",
    )

    assert first["user_id"] == second["user_id"]
    assert second["user_id"] == third["user_id"]
    assert first["is_admin"] is False
    assert second["display_name"] == "Pipeline runtime renamed"
    assert third["display_name"] == "Pipeline runtime renamed"
    assert role_sync.created_users == [uuid.UUID(first["user_id"])]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT auth_provider, account_kind, account_status, password_hash
              FROM users WHERE id = $1
            """,
            uuid.UUID(first["user_id"]),
        )
    assert row["auth_provider"] == "service"
    assert row["account_kind"] == "service"
    assert row["account_status"] == "active"
    assert row["password_hash"].startswith("!service-account:")

    from app.exceptions import AuthenticationError
    from app.services.auth_service import change_password, login

    with pytest.raises(AuthenticationError):
        await login(username, "any-local-password")

    from app.exceptions import PasswordLifecycleUnavailableError
    from app.services.password_service import reset_password

    with pytest.raises(PasswordLifecycleUnavailableError):
        await change_password(
            first["user_id"],
            "any-local-password",
            "replacement-password",
        )

    with pytest.raises(PasswordLifecycleUnavailableError):
        await reset_password(
            username=username,
            actor_id="platform-service",
            method="admin_ui",
        )

    from app.services.auth_service import create_pat, resolve_token

    credential = await create_pat(
        first["user_id"],
        "runtime-service-key",
        key_class="service",
        scopes=["read", "write"],
    )
    from app.config import settings

    monkeypatch.setattr(settings, "local_auth_enabled", False, raising=False)
    resolved = await resolve_token(f"Bearer {credential['token']}")
    assert resolved is not None
    assert resolved.user_id == first["user_id"]
    assert resolved.key_class == "service"

    async with pool.acquire() as conn:
        password_hash = await conn.fetchval(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(first["user_id"]),
        )
    assert password_hash.startswith("!service-account:")


async def test_concurrent_service_user_ensure_converges_to_one_user(services):
    pool, _, service = services
    username = f"governance-concurrent-runtime-{uuid.uuid4().hex[:8]}"
    email = f"{username}@service.akb.invalid"

    results = await asyncio.gather(
        *[
            service.ensure_service_user(
                username=username,
                email=email,
                display_name="Concurrent runtime",
                actor_id="platform-service",
            )
            for _ in range(3)
        ]
    )

    assert len({result["user_id"] for result in results}) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE username = $1", username
        ) == 1


async def test_role_projection_preserves_user_id(services):
    _, _, service = services
    ensured = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=f"role-{uuid.uuid4().hex}",
        email=f"governance-role-{uuid.uuid4().hex[:10]}@example.com",
        display_name=None,
        actor_id="platform-service",
    )

    promoted = await service.set_user_admin(
        ensured["user_id"], is_admin=True, actor_id="platform-service"
    )
    demoted = await service.set_user_admin(
        ensured["user_id"], is_admin=False, actor_id="platform-service"
    )

    assert promoted["user_id"] == ensured["user_id"]
    assert promoted["is_admin"] is True
    assert demoted["user_id"] == ensured["user_id"]
    assert demoted["is_admin"] is False


async def test_suspended_human_cannot_be_promoted_but_can_be_demoted(services):
    _, _, service = services
    ensured = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=f"suspended-role-{uuid.uuid4().hex}",
        email=f"governance-suspended-role-{uuid.uuid4().hex[:8]}@example.com",
        display_name=None,
        actor_id="platform-service",
    )
    await service.set_user_admin(
        ensured["user_id"], is_admin=True, actor_id="platform-service"
    )
    await service.suspend_user(ensured["user_id"], actor_id="platform-service")

    from app.exceptions import AccountSuspendedError

    with pytest.raises(AccountSuspendedError):
        await service.set_user_admin(
            ensured["user_id"], is_admin=True, actor_id="platform-service"
        )
    demoted = await service.set_user_admin(
        ensured["user_id"], is_admin=False, actor_id="platform-service"
    )
    assert demoted["is_admin"] is False


async def test_suspend_revokes_sessions_tokens_and_strict_token_roles(services):
    pool, role_sync, service = services
    ensured = await service.ensure_service_user(
        username=f"governance-suspend-{uuid.uuid4().hex[:10]}",
        email=f"governance-suspend-{uuid.uuid4().hex[:10]}@service.akb.invalid",
        display_name=None,
        actor_id="platform-service",
    )
    user_id = uuid.UUID(ensured["user_id"])
    token_ids: list[uuid.UUID] = []
    async with pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT tokens_revoked_before FROM users WHERE id = $1", user_id
        )
        for key_class in ("pat", "service", "publishable"):
            token_ids.append(
                await conn.fetchval(
                    """
                    INSERT INTO tokens (
                        user_id, name, token_hash, token_prefix, key_class
                    ) VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    user_id,
                    f"governance-{key_class}",
                    uuid.uuid4().hex,
                    "akb_govern",
                    key_class,
                )
            )

    result = await service.suspend_user(
        str(user_id), actor_id="platform-service"
    )

    assert result["account_status"] == "suspended"
    assert set(result["revoked_token_ids"]) == {str(token_id) for token_id in token_ids}
    assert set(role_sync.revoked_tokens) == set(token_ids)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT account_status, tokens_revoked_before,
                   (SELECT COUNT(*) FROM tokens WHERE user_id = $1) AS token_count
              FROM users WHERE id = $1
            """,
            user_id,
        )
    assert row["account_status"] == "suspended"
    assert row["tokens_revoked_before"] > before
    assert row["token_count"] == 0


async def test_failed_strict_cleanup_keeps_denial_and_retry_finishes(services):
    pool, role_sync, service = services
    ensured = await service.ensure_service_user(
        username=f"governance-retry-{uuid.uuid4().hex[:10]}",
        email=f"governance-retry-{uuid.uuid4().hex[:10]}@service.akb.invalid",
        display_name=None,
        actor_id="platform-service",
    )
    user_id = uuid.UUID(ensured["user_id"])
    async with pool.acquire() as conn:
        token_id = await conn.fetchval(
            """
            INSERT INTO tokens (user_id, name, token_hash, token_prefix, key_class)
            VALUES ($1, 'governance-retry', $2, 'akb_govern', 'service')
            RETURNING id
            """,
            user_id,
            uuid.uuid4().hex,
        )
    role_sync.fail_token = token_id

    from app.exceptions import CredentialCleanupIncompleteError

    with pytest.raises(CredentialCleanupIncompleteError):
        await service.suspend_user(str(user_id), actor_id="platform-service")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT account_status,
                   (SELECT COUNT(*) FROM tokens WHERE user_id = $1) AS token_count
              FROM users WHERE id = $1
            """,
            user_id,
        )
    assert row["account_status"] == "suspended"
    assert row["token_count"] == 0

    role_sync.fail_token = None
    retried = await service.suspend_user(str(user_id), actor_id="platform-service")
    assert retried["revoked_token_ids"] == [str(token_id)]
    assert token_id in role_sync.revoked_tokens


async def test_activation_restores_account_only_not_deleted_credentials(services):
    pool, _, service = services
    ensured = await service.ensure_service_user(
        username=f"governance-activate-{uuid.uuid4().hex[:10]}",
        email=f"governance-activate-{uuid.uuid4().hex[:10]}@service.akb.invalid",
        display_name=None,
        actor_id="platform-service",
    )
    await service.suspend_user(ensured["user_id"], actor_id="platform-service")
    activated = await service.activate_user(
        ensured["user_id"], actor_id="platform-service"
    )

    assert activated["account_status"] == "active"
    async with pool.acquire() as conn:
        token_count = await conn.fetchval(
            "SELECT COUNT(*) FROM tokens WHERE user_id = $1",
            uuid.UUID(ensured["user_id"]),
        )
    assert token_count == 0


async def test_query_by_external_identity_returns_governed_user(services):
    _, _, service = services
    subject = f"lookup-{uuid.uuid4().hex}"
    ensured = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=f"governance-lookup-{uuid.uuid4().hex[:10]}@example.com",
        display_name=None,
        actor_id="platform-service",
    )

    found = await service.get_user_by_external_identity(_ISSUER, subject)
    assert found["user_id"] == ensured["user_id"]
    assert found["account_kind"] == "human"
    assert found["account_status"] == "active"


async def test_invalid_governance_user_id_is_a_validation_error(services):
    _, _, service = services

    from app.exceptions import ValidationError

    with pytest.raises(ValidationError):
        await service.get_user("not-a-uuid")


async def test_exact_owned_token_revocation_is_strict_and_cross_user_safe(services):
    pool, role_sync, service = services
    owner = await service.ensure_service_user(
        username=f"governance-token-owner-{uuid.uuid4().hex[:8]}",
        email=f"governance-token-owner-{uuid.uuid4().hex[:8]}@service.akb.invalid",
        display_name=None,
        actor_id="platform-service",
    )
    other = await service.ensure_service_user(
        username=f"governance-token-other-{uuid.uuid4().hex[:8]}",
        email=f"governance-token-other-{uuid.uuid4().hex[:8]}@service.akb.invalid",
        display_name=None,
        actor_id="platform-service",
    )
    owner_id = uuid.UUID(owner["user_id"])
    async with pool.acquire() as conn:
        token_id = await conn.fetchval(
            """
            INSERT INTO tokens (user_id, name, token_hash, token_prefix, key_class)
            VALUES ($1, 'governance-exact', $2, 'akb_govern', 'service')
            RETURNING id
            """,
            owner_id,
            uuid.uuid4().hex,
        )

    from app.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await service.revoke_user_token(
            other["user_id"], str(token_id), actor_id="platform-service"
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT EXISTS (SELECT 1 FROM tokens WHERE id = $1)", token_id)

    revoked = await service.revoke_user_token(
        owner["user_id"], str(token_id), actor_id="platform-service"
    )
    assert revoked == {"user_id": owner["user_id"], "token_id": str(token_id), "revoked": True}
    assert token_id in role_sync.revoked_tokens
