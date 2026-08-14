"""Administrative workspace-account projection and revocation tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_ISSUER = "https://id.example.com/realms/akb"
_SSO_SESSION_EPOCH = uuid.UUID("8dbce581-ea57-47ca-ae8f-3c3ddb05e787")


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

    async with pool.acquire() as conn:
        for filename in (
            "043_workspace_account_governance.py",
            "071_recovery_admin.py",
            "072_admin_browser_sessions.py",
            "074_sso_browser_sessions.py",
            "076_sso_session_epoch.py",
        ):
            migration = _load_migration(filename)
            assert migration is not None
            await migration.migrate(conn=conn)
        await conn.execute(
            """
            INSERT INTO auth_runtime_state (
                singleton, runtime_generation, auth_mode, sso_session_epoch
            ) VALUES (TRUE, 1, 'sso', $1)
            ON CONFLICT (singleton) DO UPDATE
               SET runtime_generation = EXCLUDED.runtime_generation,
                   auth_mode = EXCLUDED.auth_mode,
                   sso_session_epoch = EXCLUDED.sso_session_epoch,
                   updated_at = NOW()
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

    role_sync = RecordingRoleSync()

    async def _get_pool():
        return pool

    monkeypatch.setattr(account_service, "get_pool", _get_pool)
    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(password_service, "get_pool", _get_pool)
    monkeypatch.setattr(account_service, "get_role_sync", lambda: role_sync)
    yield pool, role_sync, account_service

    async with pool.acquire() as conn:
        user_ids = await conn.fetch("SELECT id FROM users WHERE username LIKE 'governance-%'")
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


async def test_exact_email_lookup_returns_only_existing_human(services):
    pool, _, service = services
    user_id = uuid.uuid4()
    email = f"governance-legacy-{uuid.uuid4().hex[:10]}@example.com"
    username = f"governance-{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, display_name,
                is_admin, auth_provider, account_status, account_kind
            ) VALUES ($1, $2, $3, '!legacy!', 'Legacy member',
                      false, 'local', 'active', 'human')
            """,
            user_id,
            username,
            email,
        )

    resolved = await service.get_human_user_by_email(f"  {email.upper()}  ")

    assert resolved == {
        "user_id": str(user_id),
        "username": username,
        "email": email,
        "display_name": "Legacy member",
        "is_admin": False,
        "account_status": "active",
        "account_kind": "human",
        "auth_provider": "local",
        "has_external_identity": False,
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO external_identities (user_id, issuer, subject, email_snapshot)
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            "https://id.example.com/realms/akb",
            f"subject-{uuid.uuid4()}",
            email,
        )

    bound = await service.get_human_user_by_email(email)
    assert bound["user_id"] == str(user_id)
    assert bound["has_external_identity"] is True


async def test_exact_email_lookup_does_not_return_service_identity(services):
    pool, _, service = services
    email = f"governance-service-{uuid.uuid4().hex[:10]}@example.com"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, display_name,
                is_admin, auth_provider, account_status, account_kind
            ) VALUES ($1, $2, $3, '!service!', 'Service',
                      false, 'service', 'active', 'service')
            """,
            uuid.uuid4(),
            f"governance-{uuid.uuid4().hex}",
            email,
        )

    from app.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await service.get_human_user_by_email(email)


async def test_prepare_human_binding_is_suspended_atomically_and_not_reactivated_by_default(
    services,
):
    pool, _, service = services
    subject = f"prepared-human-{uuid.uuid4().hex}"
    email = f"governance-prepared-human-{uuid.uuid4().hex[:10]}@example.com"

    prepared = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=email,
        display_name="Prepared member",
        actor_id="platform-service",
        prepare_suspended=True,
    )
    retried_without_prepare = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=email,
        display_name="Prepared member",
        actor_id="platform-service",
    )

    assert prepared["user_id"] == retried_without_prepare["user_id"]
    assert prepared["account_status"] == "suspended"
    assert retried_without_prepare["account_status"] == "suspended"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.account_status, e.issuer, e.subject
              FROM users u
              JOIN external_identities e ON e.user_id = u.id
             WHERE u.id = $1
            """,
            uuid.UUID(prepared["user_id"]),
        )
    assert dict(row) == {
        "account_status": "suspended",
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
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM external_identities WHERE issuer = $1 AND subject = $2",
                _ISSUER,
                subject,
            )
            == 1
        )


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


async def test_create_pat_uses_caller_selected_token_id(services):
    pool, _, service = services
    requested_token_id = uuid.uuid4()
    ensured = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=f"selected-token-{uuid.uuid4().hex}",
        email=f"governance-selected-token-{uuid.uuid4().hex[:10]}@example.com",
        display_name="Selected token owner",
        actor_id="platform-service",
    )
    from app.services.auth_service import create_pat

    credential = await create_pat(
        ensured["user_id"],
        "platform-operation",
        token_id=str(requested_token_id),
    )

    assert credential["token_id"] == str(requested_token_id)
    async with pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT id FROM tokens WHERE id = $1 AND user_id = $2",
            requested_token_id,
            uuid.UUID(ensured["user_id"]),
        )
    assert stored == requested_token_id


async def test_adopt_bootstrap_admin_preserves_current_token_and_revokes_others(
    services,
    monkeypatch,
):
    pool, role_sync, service = services
    from app.config import settings
    from app.services import auth_service, recovery_admin_service

    monkeypatch.setattr(recovery_admin_service, "get_pool", service.get_pool)
    monkeypatch.setattr(recovery_admin_service, "get_role_sync", lambda: role_sync)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)

    user_id = uuid.uuid4()
    current_token_id = uuid.uuid4()
    stale_token_id = uuid.uuid4()
    username = f"governance-platform-bot-{uuid.uuid4().hex[:8]}"
    email = f"{username}@workspace.local"
    raw_current = "akb_" + uuid.uuid4().hex
    raw_stale = "akb_" + uuid.uuid4().hex
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, display_name, is_admin,
                is_recovery_admin, auth_provider, account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'Platform Bot', true,
                      true, 'local', 'active', 'human')
            """,
            user_id,
            username,
            email,
            auth_service.hash_password("legacy-password"),
        )
        await conn.executemany(
            """
            INSERT INTO tokens (
                id, user_id, name, token_hash, token_prefix, key_class
            ) VALUES ($1, $2, $3, $4, $5, 'pat')
            """,
            [
                (
                    current_token_id,
                    user_id,
                    "platform",
                    auth_service._hash_token(raw_current),
                    raw_current[:12],
                ),
                (
                    stale_token_id,
                    user_id,
                    "stale",
                    auth_service._hash_token(raw_stale),
                    raw_stale[:12],
                ),
            ],
        )

    adopted = await service.adopt_current_admin_as_service(
        user_id=str(user_id),
        token_id=str(current_token_id),
        expected_username=username,
        expected_email=email,
        actor_id=str(user_id),
    )
    assert adopted["user_id"] == str(user_id)
    assert adopted["account_kind"] == "service"
    assert adopted["auth_provider"] == "service"
    assert adopted["is_admin"] is True
    assert adopted["is_recovery_admin"] is False
    assert adopted["token_id"] == str(current_token_id)
    assert adopted["key_class"] == "service"
    assert adopted["revoked_token_ids"] == [str(stale_token_id)]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.password_hash, u.tokens_revoked_before, u.is_recovery_admin,
                   t.key_class, t.scopes,
                   (SELECT count(*) FROM tokens WHERE user_id = u.id) AS token_count
              FROM users u
              JOIN tokens t ON t.user_id = u.id AND t.id = $2
             WHERE u.id = $1
            """,
            user_id,
            current_token_id,
        )
    assert row["password_hash"].startswith("!service-account:")
    assert row["tokens_revoked_before"] is not None
    assert row["is_recovery_admin"] is False
    assert row["key_class"] == "service"
    assert list(row["scopes"]) == ["read", "write", "admin"]
    assert row["token_count"] == 1
    assert role_sync.revoked_tokens == [stale_token_id]

    async with pool.acquire() as conn:
        event_count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE kind = 'auth.bootstrap_service_adopted' AND actor_id = $1",
            str(user_id),
        )
    assert event_count == 1

    resolved = await auth_service.resolve_token(f"Bearer {raw_current}")
    assert resolved is not None
    assert resolved.user_id == str(user_id)
    assert resolved.key_class == "service"
    assert resolved.is_admin is True

    retried = await service.adopt_current_admin_as_service(
        user_id=str(user_id),
        token_id=str(current_token_id),
        expected_username=username,
        expected_email=email,
        actor_id=str(user_id),
    )
    assert retried["is_recovery_admin"] is False
    assert retried["revoked_token_ids"] == []
    assert role_sync.revoked_tokens == [stale_token_id]
    async with pool.acquire() as conn:
        retried_event_count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE kind = 'auth.bootstrap_service_adopted' AND actor_id = $1",
            str(user_id),
        )
    assert retried_event_count == event_count

    sso_recovery = await recovery_admin_service.provision_sso_recovery_admin(
        username=f"governance-sso-recovery-{uuid.uuid4().hex[:8]}",
        email=f"governance-sso-recovery-{uuid.uuid4().hex[:8]}@example.com",
        issuer=_ISSUER,
        subject=f"recovery-{uuid.uuid4().hex}",
    )
    assert sso_recovery["created"] is True
    assert sso_recovery["is_recovery_admin"] is True
    assert sso_recovery["user_id"] != str(user_id)


async def test_adopt_bootstrap_admin_rejects_identity_mismatch_atomically(services):
    pool, _, service = services
    from app.services import auth_service
    from app.exceptions import ServiceIdentityAdoptionError

    user_id = uuid.uuid4()
    token_id = uuid.uuid4()
    username = f"governance-platform-bot-{uuid.uuid4().hex[:8]}"
    email = f"{username}@workspace.local"
    raw = "akb_" + uuid.uuid4().hex
    password_hash = auth_service.hash_password("legacy-password")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, display_name, is_admin,
                auth_provider, account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'Platform Bot', true,
                      'local', 'active', 'human')
            """,
            user_id,
            username,
            email,
            password_hash,
        )
        await conn.execute(
            """
            INSERT INTO tokens (id, user_id, name, token_hash, token_prefix, key_class)
            VALUES ($1, $2, 'platform', $3, $4, 'pat')
            """,
            token_id,
            user_id,
            auth_service._hash_token(raw),
            raw[:12],
        )

    with pytest.raises(ServiceIdentityAdoptionError):
        await service.adopt_current_admin_as_service(
            user_id=str(user_id),
            token_id=str(token_id),
            expected_username=username,
            expected_email="wrong@example.com",
            actor_id=str(user_id),
        )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT account_kind, auth_provider, password_hash,
                   (SELECT key_class FROM tokens WHERE id = $2) AS key_class
              FROM users WHERE id = $1
            """,
            user_id,
            token_id,
        )
    assert dict(row) == {
        "account_kind": "human",
        "auth_provider": "local",
        "password_hash": password_hash,
        "key_class": "pat",
    }


async def test_adopt_bootstrap_admin_keeps_denial_and_retries_role_cleanup(services):
    pool, role_sync, service = services
    from app.exceptions import CredentialCleanupIncompleteError
    from app.services import auth_service

    user_id = uuid.uuid4()
    current_token_id = uuid.uuid4()
    stale_token_id = uuid.uuid4()
    username = f"governance-platform-bot-{uuid.uuid4().hex[:8]}"
    email = f"{username}@workspace.local"
    current_raw = "akb_" + uuid.uuid4().hex
    stale_raw = "akb_" + uuid.uuid4().hex
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, display_name, is_admin,
                auth_provider, account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'Platform Bot', true,
                      'local', 'active', 'human')
            """,
            user_id,
            username,
            email,
            auth_service.hash_password("legacy-password"),
        )
        await conn.executemany(
            """
            INSERT INTO tokens (id, user_id, name, token_hash, token_prefix, key_class)
            VALUES ($1, $2, $3, $4, $5, 'pat')
            """,
            [
                (current_token_id, user_id, "platform", auth_service._hash_token(current_raw), current_raw[:12]),
                (stale_token_id, user_id, "stale", auth_service._hash_token(stale_raw), stale_raw[:12]),
            ],
        )

    role_sync.fail_token = stale_token_id
    with pytest.raises(CredentialCleanupIncompleteError):
        await service.adopt_current_admin_as_service(
            user_id=str(user_id),
            token_id=str(current_token_id),
            expected_username=username,
            expected_email=email,
            actor_id=str(user_id),
        )

    async with pool.acquire() as conn:
        state = await conn.fetchrow(
            """
            SELECT u.account_kind, u.auth_provider,
                   EXISTS (SELECT 1 FROM tokens WHERE id = $2) AS current_exists,
                   EXISTS (SELECT 1 FROM tokens WHERE id = $3) AS stale_exists,
                   EXISTS (
                       SELECT 1 FROM account_token_cleanup
                        WHERE token_id = $3 AND completed_at IS NULL
                   ) AS cleanup_pending
              FROM users u WHERE u.id = $1
            """,
            user_id,
            current_token_id,
            stale_token_id,
        )
    assert dict(state) == {
        "account_kind": "service",
        "auth_provider": "service",
        "current_exists": True,
        "stale_exists": False,
        "cleanup_pending": True,
    }

    role_sync.fail_token = None
    retried = await service.adopt_current_admin_as_service(
        user_id=str(user_id),
        token_id=str(current_token_id),
        expected_username=username,
        expected_email=email,
        actor_id=str(user_id),
    )
    assert retried["revoked_token_ids"] == [str(stale_token_id)]
    async with pool.acquire() as conn:
        completed = await conn.fetchval(
            "SELECT completed_at IS NOT NULL FROM account_token_cleanup WHERE token_id = $1",
            stale_token_id,
        )
    assert completed is True


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
        assert await conn.fetchval("SELECT COUNT(*) FROM users WHERE username = $1", username) == 1


async def test_role_projection_preserves_user_id(services):
    pool, _, service = services
    ensured = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=f"role-{uuid.uuid4().hex}",
        email=f"governance-role-{uuid.uuid4().hex[:10]}@example.com",
        display_name=None,
        actor_id="platform-service",
    )

    promoted = await service.set_user_admin(ensured["user_id"], is_admin=True, actor_id="platform-service")
    async with pool.acquire() as conn:
        identity = await conn.fetchrow(
            "SELECT id, issuer, subject FROM external_identities WHERE user_id = $1",
            uuid.UUID(ensured["user_id"]),
        )
        await conn.execute(
            """
            INSERT INTO admin_browser_sessions (
                session_epoch, token_hash, csrf_token_hash, user_id, external_identity_id,
                identity_issuer, identity_subject, keycloak_sid, expires_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() + INTERVAL '5 minutes')
            """,
            _SSO_SESSION_EPOCH,
            uuid.uuid4().hex * 2,
            uuid.uuid4().hex * 2,
            uuid.UUID(ensured["user_id"]),
            identity["id"],
            identity["issuer"],
            identity["subject"],
            "admin-session-before-demotion",
        )
    demoted = await service.set_user_admin(ensured["user_id"], is_admin=False, actor_id="platform-service")

    assert promoted["user_id"] == ensured["user_id"]
    assert promoted["is_admin"] is True
    assert demoted["user_id"] == ensured["user_id"]
    assert demoted["is_admin"] is False
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM admin_browser_sessions WHERE user_id = $1",
                uuid.UUID(ensured["user_id"]),
            )
            == 0
        )


async def test_suspended_human_cannot_be_promoted_but_can_be_demoted(services):
    _, _, service = services
    ensured = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=f"suspended-role-{uuid.uuid4().hex}",
        email=f"governance-suspended-role-{uuid.uuid4().hex[:8]}@example.com",
        display_name=None,
        actor_id="platform-service",
    )
    await service.set_user_admin(ensured["user_id"], is_admin=True, actor_id="platform-service")
    await service.suspend_user(ensured["user_id"], actor_id="platform-service")

    from app.exceptions import AccountSuspendedError

    with pytest.raises(AccountSuspendedError):
        await service.set_user_admin(ensured["user_id"], is_admin=True, actor_id="platform-service")
    demoted = await service.set_user_admin(ensured["user_id"], is_admin=False, actor_id="platform-service")
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
        before = await conn.fetchval("SELECT tokens_revoked_before FROM users WHERE id = $1", user_id)
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

    result = await service.suspend_user(str(user_id), actor_id="platform-service")

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


async def test_prepare_existing_human_suspends_and_strictly_revokes_tokens(services):
    pool, role_sync, service = services
    subject = f"prepared-existing-{uuid.uuid4().hex}"
    email = f"governance-prepared-existing-{uuid.uuid4().hex[:10]}@example.com"
    ensured = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=email,
        display_name="Existing member",
        actor_id="platform-service",
    )
    user_id = uuid.UUID(ensured["user_id"])
    async with pool.acquire() as conn:
        token_id = await conn.fetchval(
            """
            INSERT INTO tokens (user_id, name, token_hash, token_prefix, key_class)
            VALUES ($1, 'governance-prepare', $2, 'akb_govern', 'pat')
            RETURNING id
            """,
            user_id,
            uuid.uuid4().hex,
        )

    prepared = await service.ensure_human_external_identity(
        issuer=_ISSUER,
        subject=subject,
        email=email,
        display_name="Existing member",
        actor_id="platform-service",
        prepare_suspended=True,
    )

    assert prepared["account_status"] == "suspended"
    assert role_sync.revoked_tokens == [token_id]
    async with pool.acquire() as conn:
        token_count = await conn.fetchval(
            "SELECT COUNT(*) FROM tokens WHERE user_id = $1",
            user_id,
        )
        cleanup_completed = await conn.fetchval(
            """
            SELECT completed_at IS NOT NULL
              FROM account_token_cleanup
             WHERE token_id = $1 AND user_id = $2
            """,
            token_id,
            user_id,
        )
    assert token_count == 0
    assert cleanup_completed is True


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
    activated = await service.activate_user(ensured["user_id"], actor_id="platform-service")

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
        await service.revoke_user_token(other["user_id"], str(token_id), actor_id="platform-service")
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT EXISTS (SELECT 1 FROM tokens WHERE id = $1)", token_id)

    revoked = await service.revoke_user_token(owner["user_id"], str(token_id), actor_id="platform-service")
    assert revoked == {"user_id": owner["user_id"], "token_id": str(token_id), "revoked": True}
    assert token_id in role_sync.revoked_tokens


async def test_admin_identifies_expired_suspended_legacy_token_without_secret_leak(services):
    pool, _, service = services
    owner = await service.ensure_service_user(
        username=f"governance-token-identify-{uuid.uuid4().hex[:8]}",
        email=f"governance-token-identify-{uuid.uuid4().hex[:8]}@service.akb.invalid",
        display_name=None,
        actor_id="platform-service",
    )
    owner_id = uuid.UUID(owner["user_id"])
    raw_token = "akb_legacy-secret-material-" + uuid.uuid4().hex
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    async with pool.acquire() as conn:
        token_id = await conn.fetchval(
            """
            INSERT INTO tokens (
                user_id, name, token_hash, token_prefix, key_class, expires_at
            ) VALUES ($1, 'legacy-platform-bridge', $2, 'akb_legacy', 'pat', NOW() - INTERVAL '1 day')
            RETURNING id
            """,
            owner_id,
            token_hash,
        )
        await conn.execute(
            "UPDATE users SET account_status = 'suspended' WHERE id = $1",
            owner_id,
        )

    identified = await service.identify_user_token(
        raw_token,
        actor_id="platform-service",
    )
    assert identified == {
        "user_id": owner["user_id"],
        "token_id": str(token_id),
    }
    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            """
            SELECT actor_id, payload::text
              FROM events
             WHERE kind = 'auth.token_identified'
               AND payload->>'token_id' = $1
             ORDER BY id DESC LIMIT 1
            """,
            str(token_id),
        )
    assert event["actor_id"] == "platform-service"
    assert raw_token not in event["payload"]
    assert token_hash not in event["payload"]

    from app.exceptions import NotFoundError

    with pytest.raises(NotFoundError) as exc_info:
        await service.identify_user_token(
            raw_token + "-unknown",
            actor_id="platform-service",
        )
    assert raw_token not in str(exc_info.value)
    assert token_hash not in str(exc_info.value)
