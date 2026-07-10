"""Unit coverage for the vault_write_policy / vault_write_grants substrate.

P0 slice S3: two sidecar tables + a repo + the ``current_token_id``
ContextVar (already shipped alongside ``current_vault_scope`` for the
PG-native RBAC role-switch — this file additionally pins its PAT/JWT/reset
contract for the vault_write_policy consumer). DB-backed: migration 044
and the repo are pure SQL, so these tests apply init.sql + migration 044
onto a real Postgres (``AKB_TEST_DSN``, same convention as
``test_account_status_auth_carriers.py`` / ``test_workspace_account_admin_service.py``)
and self-skip if unreachable.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.config import settings


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_INIT_SQL_PATH = Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"


@pytest.fixture
async def pool(monkeypatch):
    try:
        pg_pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=5)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    # Fresh-DB bootstrap: init.sql already has every prior migration's
    # schema folded forward (see backend/app/db/init.sql), so applying it
    # once + migration 044 on top is equivalent to the full ledger chain.
    async with pg_pool.acquire() as conn:
        await conn.execute(_INIT_SQL_PATH.read_text())

    from app.db.postgres import _load_migration
    from app.repositories import vault_write_policy_repo
    from app.services import auth_service

    migration = _load_migration("044_vault_write_policy.py")
    assert migration is not None
    async with pg_pool.acquire() as conn:
        await migration.migrate(conn=conn)

    async def _get_pool():
        return pg_pool

    # Both modules resolve `get_pool` as a name bound into their own
    # namespace at import time (`from app.db.postgres import get_pool`),
    # so each needs its own patch target — patching only one leaves the
    # other's conn=None fallback hitting the real (unreachable-from-here)
    # app.db.postgres singleton pool.
    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(vault_write_policy_repo, "get_pool", _get_pool)
    monkeypatch.setattr(
        settings, "jwt_secret", "vwp-test-secret-at-least-32-bytes-long", raising=False
    )

    yield pg_pool

    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM vaults WHERE name LIKE 'vwp-%'")
        await conn.execute("DELETE FROM users WHERE username LIKE 'vwp-%'")
    await pg_pool.close()


async def _create_user(pg_pool) -> uuid.UUID:
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash) "
            "VALUES ($1, $2, $3, 'x')",
            user_id,
            f"vwp-{suffix}",
            f"vwp-{suffix}@example.com",
        )
    return user_id


async def _create_vault(pg_pool, owner_id: uuid.UUID) -> uuid.UUID:
    vault_id = uuid.uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vaults (id, name, git_path, owner_id) VALUES ($1, $2, $3, $4)",
            vault_id,
            f"vwp-vault-{uuid.uuid4().hex[:10]}",
            f"/tmp/vwp-{vault_id}.git",
            owner_id,
        )
    return vault_id


async def _create_token(pg_pool, user_id: uuid.UUID) -> uuid.UUID:
    from app.services.auth_service import _hash_token

    raw = f"akb_vwp_{uuid.uuid4().hex}"
    async with pg_pool.acquire() as conn:
        token_id = await conn.fetchval(
            "INSERT INTO tokens (user_id, name, token_hash, token_prefix) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            user_id,
            "vwp-test-token",
            _hash_token(raw),
            raw[:12],
        )
    return token_id


class TestMigrationIdempotent:
    async def test_migrate_applies_twice_without_error(self, pool):
        from app.db.postgres import _load_migration

        migration = _load_migration("044_vault_write_policy.py")
        async with pool.acquire() as conn:
            await migration.migrate(conn=conn)  # 2nd application (fixture already ran it once)

        async with pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('vault_write_policy', 'vault_write_grants')"
            )
        assert {r["table_name"] for r in tables} == {
            "vault_write_policy",
            "vault_write_grants",
        }

    async def test_migrate_twice_preserves_existing_rows(self, pool):
        from app.db.postgres import _load_migration

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO vault_write_policy (vault_id, managed_by, created_by) "
                "VALUES ($1, 'collector:test', 'tester')",
                vault_id,
            )

        migration = _load_migration("044_vault_write_policy.py")
        async with pool.acquire() as conn:
            await migration.migrate(conn=conn)

        async with pool.acquire() as conn:
            managed_by = await conn.fetchval(
                "SELECT managed_by FROM vault_write_policy WHERE vault_id = $1",
                vault_id,
            )
        assert managed_by == "collector:test"


class TestRepoCrudRoundTrip:
    async def test_get_policy_none_when_ungoverned(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)

        assert await repo.get_policy(vault_id) is None

    async def test_set_then_get_policy_round_trips(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)

        created = await repo.set_policy(
            vault_id, "collector:acme-jira", "admin-user", note="pilot vault"
        )
        assert created["vault_id"] == vault_id
        assert created["managed_by"] == "collector:acme-jira"
        assert created["note"] == "pilot vault"
        assert created["created_by"] == "admin-user"

        fetched = await repo.get_policy(vault_id)
        assert fetched is not None
        assert fetched["managed_by"] == "collector:acme-jira"
        assert fetched["note"] == "pilot vault"

    async def test_set_policy_upserts_without_disturbing_created_by(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)

        await repo.set_policy(vault_id, "collector:v1", "first-admin")
        updated = await repo.set_policy(vault_id, "collector:v2", "second-admin", note="re-marked")

        assert updated["managed_by"] == "collector:v2"
        assert updated["note"] == "re-marked"
        # created_by is pinned to the original mark, not overwritten by the upsert.
        assert updated["created_by"] == "first-admin"

    async def test_remove_policy_clears_it(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:x", "admin-user")

        await repo.remove_policy(vault_id)

        assert await repo.get_policy(vault_id) is None

    async def test_is_granted_false_then_add_grant_true_then_remove_false(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)
        await repo.set_policy(vault_id, "collector:x", "admin-user")

        assert await repo.is_granted(vault_id, token_id) is False

        await repo.add_grant(vault_id, token_id, "admin-user")
        assert await repo.is_granted(vault_id, token_id) is True

        await repo.remove_grant(vault_id, token_id)
        assert await repo.is_granted(vault_id, token_id) is False

    async def test_add_grant_is_idempotent(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)
        await repo.set_policy(vault_id, "collector:x", "admin-user")

        await repo.add_grant(vault_id, token_id, "admin-user")
        await repo.add_grant(vault_id, token_id, "admin-user")  # must not raise

        assert await repo.is_granted(vault_id, token_id) is True

    async def test_add_grant_without_policy_raises_fk_violation(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)  # no set_policy call
        token_id = await _create_token(pool, owner_id)

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await repo.add_grant(vault_id, token_id, "admin-user")

    async def test_repo_functions_accept_an_explicit_conn(self, pool):
        """Callers embedding this in a larger transaction pass ``conn=``."""
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)

        async with pool.acquire() as conn:
            async with conn.transaction():
                await repo.set_policy(vault_id, "collector:x", "admin-user", conn=conn)
                await repo.add_grant(vault_id, token_id, "admin-user", conn=conn)
                assert await repo.is_granted(vault_id, token_id, conn=conn) is True
                assert (await repo.get_policy(vault_id, conn=conn))["managed_by"] == "collector:x"


class TestCascade:
    async def test_deleting_token_removes_its_grant(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)
        await repo.set_policy(vault_id, "collector:x", "admin-user")
        await repo.add_grant(vault_id, token_id, "admin-user")
        assert await repo.is_granted(vault_id, token_id) is True

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM tokens WHERE id = $1", token_id)

        assert await repo.is_granted(vault_id, token_id) is False

    async def test_deleting_policy_removes_all_its_grants(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_a = await _create_token(pool, owner_id)
        token_b = await _create_token(pool, owner_id)
        await repo.set_policy(vault_id, "collector:x", "admin-user")
        await repo.add_grant(vault_id, token_a, "admin-user")
        await repo.add_grant(vault_id, token_b, "admin-user")

        await repo.remove_policy(vault_id)

        async with pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM vault_write_grants WHERE vault_id = $1", vault_id
            )
        assert remaining == 0

    async def test_deleting_vault_removes_policy_and_grants(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)
        await repo.set_policy(vault_id, "collector:x", "admin-user")
        await repo.add_grant(vault_id, token_id, "admin-user")

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)

        assert await repo.get_policy(vault_id) is None
        async with pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM vault_write_grants WHERE vault_id = $1", vault_id
            )
        assert remaining == 0


class TestContextVar:
    """current_token_id already exists (shipped for the PG-native RBAC
    role-switch, ``app/models/vault_scope.py``) and is already set/reset
    at exactly the sites vault_write_policy's guard needs — this class
    pins that contract for this second consumer rather than driving new
    production code.
    """

    async def test_pat_resolution_sets_current_token_id(self, pool):
        from app.models.vault_scope import current_token_id
        from app.services.auth_service import resolve_token

        owner_id = await _create_user(pool)
        raw = f"akb_vwp_ctxvar_{uuid.uuid4().hex}"
        from app.services.auth_service import _hash_token

        async with pool.acquire() as conn:
            token_id = await conn.fetchval(
                "INSERT INTO tokens (user_id, name, token_hash, token_prefix) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                owner_id,
                "vwp-ctxvar-token",
                _hash_token(raw),
                raw[:12],
            )

        result = await resolve_token(f"Bearer {raw}")

        assert result is not None
        assert current_token_id.get() == str(token_id)

    async def test_jwt_resolution_leaves_token_id_none(self, pool):
        from app.models.vault_scope import current_token_id
        from app.services.auth_service import create_jwt, resolve_token

        owner_id = await _create_user(pool)
        token = create_jwt(str(owner_id), "vwp-jwt-user")

        marker = current_token_id.set("stale-marker-must-be-cleared")
        try:
            result = await resolve_token(f"Bearer {token}")
            assert result is not None
            assert current_token_id.get() is None
        finally:
            current_token_id.reset(marker)

    async def test_resolve_token_reset_clears_stale_value_even_on_failure(self, pool):
        from app.models.vault_scope import current_token_id
        from app.services.auth_service import resolve_token

        marker = current_token_id.set("stale-marker-must-be-cleared")
        try:
            result = await resolve_token("Bearer not-a-real-token-at-all")
            assert result is None
            assert current_token_id.get() is None
        finally:
            current_token_id.reset(marker)
