"""PostgreSQL contract for exact old/new external-identity continuity."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_OLD_ISSUER = "https://upstream.example.com/realms/workforce"
_NEW_ISSUER = "https://broker.example.com/realms/akb"


async def test_migration_rejects_non_utf8_subject_before_database(monkeypatch):
    from app.sso import identity_migration

    async def _must_not_get_pool():
        raise AssertionError("invalid identity must fail before database access")

    monkeypatch.setattr(identity_migration, "get_pool", _must_not_get_pool)
    with pytest.raises(identity_migration.IdentityMigrationError) as captured:
        await identity_migration.inspect_identity_migration(
            existing_user_id=str(uuid.uuid4()),
            old_issuer=_OLD_ISSUER,
            old_subject="\ud800",
            new_issuer=_NEW_ISSUER,
            new_subject="new-subject",
        )

    assert captured.value.code == "identity_migration_subject_invalid"


@pytest.fixture
async def migration_store(monkeypatch):
    try:
        admin = await asyncpg.connect(_DSN, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(
                "REQUIRE_REAL_PG=1 but Postgres is not reachable at AKB_TEST_DSN"
            )
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    schema = "sso_identity_migration_" + uuid.uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        _DSN,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
    )
    events: list[tuple[str, str, dict[str, object]]] = []
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE users (
                    id UUID PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    is_admin BOOLEAN NOT NULL DEFAULT false,
                    auth_provider TEXT NOT NULL,
                    account_status TEXT NOT NULL,
                    account_kind TEXT NOT NULL
                );
                CREATE TABLE external_identities (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    username_snapshot TEXT,
                    email_snapshot TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (issuer, subject)
                );
                CREATE TABLE tokens (
                    id UUID PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL
                );
                CREATE TABLE vaults (
                    id UUID PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    owner_id UUID REFERENCES users(id)
                );
                CREATE TABLE vault_access (
                    vault_id UUID NOT NULL REFERENCES vaults(id),
                    user_id UUID NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL,
                    UNIQUE (vault_id, user_id)
                );
                """
            )

        from app.sso import identity_migration

        async def _get_pool():
            return pool

        async def _emit_event(conn, event_type, *, actor_id, payload):
            events.append((event_type, actor_id, payload))

        monkeypatch.setattr(identity_migration, "get_pool", _get_pool)
        monkeypatch.setattr(identity_migration, "emit_event", _emit_event)
        yield pool, events, identity_migration
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _seed_continuity_state(pool):
    user_id = uuid.uuid4()
    token_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, display_name, is_admin,
                auth_provider, account_status, account_kind
            ) VALUES ($1, 'alice', 'alice@example.com', 'Alice', false,
                      'keycloak', 'active', 'human')
            """,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO external_identities (
                user_id, issuer, subject, username_snapshot, email_snapshot
            ) VALUES ($1, $2, 'old-subject', 'alice', 'alice@example.com')
            """,
            user_id,
            _OLD_ISSUER,
        )
        await conn.execute(
            """
            INSERT INTO tokens (id, user_id, name, token_hash, token_prefix)
            VALUES ($1, $2, 'continuity-pat', $3, 'akb_test')
            """,
            token_id,
            user_id,
            uuid.uuid4().hex,
        )
        await conn.execute(
            "INSERT INTO vaults (id, name, owner_id) VALUES ($1, 'owned', $2)",
            vault_id,
            user_id,
        )
        await conn.execute(
            "INSERT INTO vault_access VALUES ($1, $2, 'writer')",
            vault_id,
            user_id,
        )
    return user_id, token_id, vault_id


async def test_exact_migration_is_dry_run_first_idempotent_and_preserves_account_state(
    migration_store,
):
    pool, events, service = migration_store
    user_id, token_id, vault_id = await _seed_continuity_state(pool)

    before = await service.inspect_identity_migration(
        existing_user_id=str(user_id),
        old_issuer=_OLD_ISSUER,
        old_subject="old-subject",
        new_issuer=_NEW_ISSUER,
        new_subject="new-subject",
    )
    assert before.state == "ready_to_link"
    assert events == []

    first, second = await asyncio.gather(
        *[
            service.apply_identity_migration(
                existing_user_id=str(user_id),
                old_issuer=_OLD_ISSUER,
                old_subject="old-subject",
                new_issuer=_NEW_ISSUER,
                new_subject="new-subject",
                actor_id="product-admin",
            )
            for _ in range(2)
        ]
    )
    assert first.state == second.state == "linked"
    assert sorted([first.binding_changed, second.binding_changed]) == [False, True]

    async with pool.acquire() as conn:
        bindings = await conn.fetch(
            """
            SELECT user_id, issuer, subject, username_snapshot, email_snapshot
              FROM external_identities
             WHERE user_id = $1
             ORDER BY issuer
            """,
            user_id,
        )
        user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        token_owner = await conn.fetchval("SELECT user_id FROM tokens WHERE id = $1", token_id)
        vault_owner = await conn.fetchval("SELECT owner_id FROM vaults WHERE id = $1", vault_id)
        vault_role = await conn.fetchval(
            "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            vault_id,
            user_id,
        )

    assert [(row["issuer"], row["subject"]) for row in bindings] == [
        (_NEW_ISSUER, "new-subject"),
        (_OLD_ISSUER, "old-subject"),
    ]
    assert all(row["user_id"] == user_id for row in bindings)
    assert bindings[0]["username_snapshot"] == "alice"
    assert bindings[0]["email_snapshot"] == "alice@example.com"
    assert (user["username"], user["email"], user["display_name"]) == (
        "alice",
        "alice@example.com",
        "Alice",
    )
    assert token_owner == vault_owner == user_id
    assert vault_role == "writer"
    assert events == [
        (
            "auth.external_identity_migrated",
            "product-admin",
            {
                "user_id": str(user_id),
                "old_issuer": _OLD_ISSUER,
                "old_subject": "old-subject",
                "new_issuer": _NEW_ISSUER,
                "new_subject": "new-subject",
            },
        )
    ]

    rolled_back = await asyncio.gather(
        *[
            service.rollback_identity_migration(
                existing_user_id=str(user_id),
                old_issuer=_OLD_ISSUER,
                old_subject="old-subject",
                new_issuer=_NEW_ISSUER,
                new_subject="new-subject",
                actor_id="product-admin",
            )
            for _ in range(2)
        ]
    )
    assert [item.state for item in rolled_back] == [
        "ready_to_link",
        "ready_to_link",
    ]
    assert sorted(item.binding_changed for item in rolled_back) == [False, True]
    async with pool.acquire() as conn:
        remaining = await conn.fetch(
            "SELECT issuer, subject FROM external_identities WHERE user_id = $1",
            user_id,
        )
        assert await conn.fetchval(
            "SELECT user_id FROM tokens WHERE id = $1",
            token_id,
        ) == user_id
        assert await conn.fetchval(
            "SELECT owner_id FROM vaults WHERE id = $1",
            vault_id,
        ) == user_id
        assert await conn.fetchval(
            "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            vault_id,
            user_id,
        ) == "writer"
    assert [(row["issuer"], row["subject"]) for row in remaining] == [
        (_OLD_ISSUER, "old-subject")
    ]
    assert events[-1] == (
        "auth.external_identity_migration_rolled_back",
        "product-admin",
        {
            "user_id": str(user_id),
            "old_issuer": _OLD_ISSUER,
            "old_subject": "old-subject",
            "new_issuer": _NEW_ISSUER,
            "new_subject": "new-subject",
        },
    )
    assert [event[0] for event in events] == [
        "auth.external_identity_migrated",
        "auth.external_identity_migration_rolled_back",
    ]


async def test_migration_never_adopts_by_email_or_crosses_an_existing_binding(
    migration_store,
):
    pool, events, service = migration_store
    user_id, _, _ = await _seed_continuity_state(pool)
    other_user_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, display_name, is_admin,
                auth_provider, account_status, account_kind
            ) VALUES ($1, 'other', 'other@example.com', 'Other', false,
                      'keycloak', 'active', 'human')
            """,
            other_user_id,
        )
        await conn.execute(
            """
            INSERT INTO external_identities (user_id, issuer, subject)
            VALUES ($1, $2, 'claimed-new-subject')
            """,
            other_user_id,
            _NEW_ISSUER,
        )

    with pytest.raises(service.IdentityMigrationError) as captured:
        await service.apply_identity_migration(
            existing_user_id=str(user_id),
            old_issuer=_OLD_ISSUER,
            old_subject="old-subject",
            new_issuer=_NEW_ISSUER,
            new_subject="claimed-new-subject",
            actor_id="product-admin",
        )

    assert captured.value.code == "identity_migration_new_binding_conflict"
    assert events == []
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM external_identities WHERE user_id = $1",
            user_id,
        ) == 1


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("account_status", "suspended", "identity_migration_target_inactive"),
        ("account_kind", "service", "identity_migration_target_not_human"),
        ("auth_provider", "local", "identity_migration_target_not_external"),
    ],
)
async def test_migration_requires_the_exact_active_external_human(
    migration_store,
    column,
    value,
    code,
):
    pool, _, service = migration_store
    user_id, _, _ = await _seed_continuity_state(pool)
    async with pool.acquire() as conn:
        await conn.execute(f'UPDATE users SET "{column}" = $2 WHERE id = $1', user_id, value)

    with pytest.raises(service.IdentityMigrationError) as captured:
        await service.inspect_identity_migration(
            existing_user_id=str(user_id),
            old_issuer=_OLD_ISSUER,
            old_subject="old-subject",
            new_issuer=_NEW_ISSUER,
            new_subject="new-subject",
        )

    assert captured.value.code == code
