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

import json
import os
import uuid
from contextlib import contextmanager
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

    # Fresh-DB bootstrap: init.sql folds forward MOST prior migrations'
    # schema, but not all — notably NOT `vault_external_git` (010) or the
    # `events` outbox (015). Task 8's tests never exercised
    # `check_vault_access` or `emit_event`, so 044-only was sufficient
    # then; Task 9's guard calls both (the pre-existing external-git
    # mirror check in `check_vault_access` unconditionally queries
    # `vault_external_git` for any writer-role check, and the guard's own
    # audit event needs `events`), so those two are applied here too.
    async with pg_pool.acquire() as conn:
        await conn.execute(_INIT_SQL_PATH.read_text())

    from app.db.postgres import _load_migration
    from app.repositories import vault_write_policy_repo
    from app.services import access_service, auth_service, table_service

    for filename in (
        "010_external_git_mirror.py",
        "015_events_outbox.py",
        "044_vault_write_policy.py",
        "045_vault_write_grant_actions.py",
    ):
        migration = _load_migration(filename)
        assert migration is not None
        async with pg_pool.acquire() as conn:
            await migration.migrate(conn=conn)

    async def _get_pool():
        return pg_pool

    # Every module resolves `get_pool` as a name bound into its own
    # namespace at import time (`from app.db.postgres import get_pool`),
    # so each needs its own patch target — patching only one leaves the
    # others' conn=None fallback hitting the real (unreachable-from-here)
    # app.db.postgres singleton pool. access_service/table_service are
    # needed from Task 9 on (the write-policy guard + akb_sql dual
    # enforcement live there).
    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(vault_write_policy_repo, "get_pool", _get_pool)
    monkeypatch.setattr(access_service, "get_pool", _get_pool)
    monkeypatch.setattr(table_service, "get_pool", _get_pool)
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


async def _create_admin_user(pg_pool) -> uuid.UUID:
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES ($1, $2, $3, 'x', TRUE)",
            user_id,
            f"vwp-admin-{suffix}",
            f"vwp-admin-{suffix}@example.com",
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


async def _create_named_vault(pg_pool, owner_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    """Like `_create_vault` but also returns the vault name — the guard
    tests key off the name (`check_vault_access`/`execute_sql` both take
    vault names, not ids)."""
    vault_id = await _create_vault(pg_pool, owner_id)
    async with pg_pool.acquire() as conn:
        name = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)
    return vault_id, name


@contextmanager
def _auth_context(token_id: uuid.UUID | str | None = None, scope=None):
    """Simulate the request-scoped ContextVars `auth_service.resolve_token`
    sets per-request (`app/models/vault_scope.py`). A JWT session leaves
    both at their defaults (``None``); a PAT sets ``current_token_id`` to
    the token's id (stringified, per `_resolve_pat`) and
    ``current_vault_scope`` to its scope (``None`` if unscoped).

    Resets via ``.reset(...)`` in ``finally`` — pytest-asyncio tasks in
    this suite do not get a fresh ``contextvars.Context`` per test (see
    ``TestContextVar`` above, which relies on the same fact), so a stray
    ``.set()`` would otherwise leak into a later test.
    """
    from app.models.vault_scope import current_token_id, current_vault_scope

    t1 = current_token_id.set(str(token_id) if token_id is not None else None)
    t2 = current_vault_scope.set(scope)
    try:
        yield
    finally:
        current_token_id.reset(t1)
        current_vault_scope.reset(t2)


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


async def _create_upload_service_token(
    pg_pool,
    user_id: uuid.UUID,
    vault_name: str,
    *,
    scopes: tuple[str, ...] = ("write",),
    scoped_vault: str | None = None,
    is_admin: bool = False,
) -> uuid.UUID:
    from app.services.auth_service import _hash_token

    raw = f"akb_secret_vwp_{uuid.uuid4().hex}"
    vault_scope = (
        json.dumps({"prefixes": [], "extra_vaults": [scoped_vault or vault_name]})
        if scoped_vault != ""
        else None
    )
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET account_kind = 'service', is_admin = $2 WHERE id = $1",
            user_id,
            is_admin,
        )
        return await conn.fetchval(
            """
            INSERT INTO tokens (
                user_id, name, token_hash, token_prefix,
                scopes, vault_scope, key_class
            )
            VALUES ($1, 'vwp-upload-token', $2, $3, $4, $5::jsonb, 'service')
            RETURNING id
            """,
            user_id,
            _hash_token(raw),
            raw[:12],
            list(scopes),
            vault_scope,
        )


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
    async def test_migration_045_backfills_preexisting_grants_to_wildcard(self, pool):
        from app.db.postgres import _load_migration

        migration = _load_migration("045_vault_write_grant_actions.py")
        assert migration is not None
        schema = f"vwp_m045_{uuid.uuid4().hex}"
        vault_id = uuid.uuid4()
        token_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET search_path TO "{schema}"')
            try:
                await conn.execute(
                    """
                    CREATE TABLE vault_write_grants (
                        vault_id UUID NOT NULL,
                        token_id UUID NOT NULL,
                        granted_by TEXT NOT NULL,
                        PRIMARY KEY (vault_id, token_id)
                    )
                    """
                )
                await conn.execute(
                    "INSERT INTO vault_write_grants (vault_id, token_id, granted_by) "
                    "VALUES ($1, $2, 'pre-045-admin')",
                    vault_id,
                    token_id,
                )

                await migration.migrate(conn=conn)

                assert await conn.fetchval(
                    "SELECT write_actions FROM vault_write_grants "
                    "WHERE vault_id = $1 AND token_id = $2",
                    vault_id,
                    token_id,
                ) == ["*"]
                assert await conn.fetchval(
                    "SELECT is_nullable = 'NO' FROM information_schema.columns "
                    "WHERE table_schema = $1 "
                    "AND table_name = 'vault_write_grants' "
                    "AND column_name = 'write_actions'",
                    schema,
                ) is True
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        "UPDATE vault_write_grants SET write_actions = ARRAY[]::TEXT[] "
                        "WHERE vault_id = $1 AND token_id = $2",
                        vault_id,
                        token_id,
                    )
            finally:
                await conn.execute("RESET search_path")
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

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

    async def test_action_limited_grant_only_matches_declared_action(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)
        await repo.set_policy(vault_id, "naut:upload", "admin-user")

        await repo.add_grant(
            vault_id,
            token_id,
            "admin-user",
            write_actions=("file_upload",),
        )

        assert await repo.is_granted(vault_id, token_id, action="file_upload") is True
        assert await repo.is_granted(vault_id, token_id, action="document_write") is False
        assert await repo.is_granted(vault_id, token_id) is False
        assert await repo.get_grant_actions(vault_id, token_id) == frozenset({"file_upload"})

    async def test_regrant_replaces_wildcard_with_action_limit(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)
        await repo.set_policy(vault_id, "naut:upload", "admin-user")

        await repo.add_grant(vault_id, token_id, "admin-user")
        await repo.add_grant(
            vault_id,
            token_id,
            "admin-user",
            write_actions=("file_upload",),
        )

        assert await repo.get_grant_actions(vault_id, token_id) == frozenset({"file_upload"})
        assert await repo.is_granted(vault_id, token_id) is False

    async def test_existing_default_grant_is_wildcard_compatible(self, pool):
        from app.repositories import vault_write_policy_repo as repo

        owner_id = await _create_user(pool)
        vault_id = await _create_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)
        await repo.set_policy(vault_id, "collector:x", "admin-user")
        await repo.add_grant(vault_id, token_id, "admin-user")

        assert await repo.get_grant_actions(vault_id, token_id) == frozenset({"*"})
        assert await repo.is_granted(vault_id, token_id) is True
        assert await repo.is_granted(vault_id, token_id, action="file_upload") is True
        assert await repo.is_granted(vault_id, token_id, action="anything") is True

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


class TestWritePolicyGuard:
    """Task 9 — the enforcement guard in
    ``access_service.check_vault_access``. Fires only for mutating roles
    (writer/admin/owner) on a MARKED vault (a ``vault_write_policy`` row
    exists): a granted PAT passes, an *unscoped* system admin bypasses
    (+ a loud audit event), and everyone else — including the vault
    OWNER and a JWT session — is denied. An unmarked vault is a
    guaranteed no-op (existing behaviour, zero change).
    """

    async def test_jwt_session_write_to_marked_vault_is_403(self, pool):
        """A2 core: a plain member with a real 'writer' ACL role, but no
        PAT at all (the JWT-session shape — ``current_token_id`` stays at
        its ``None`` default), is denied on a marked vault."""
        from app.exceptions import ForbiddenError
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import check_vault_access

        owner_id = await _create_user(pool)
        member_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO vault_access (vault_id, user_id, role) VALUES ($1, $2, 'writer')",
                vault_id, member_id,
            )

        with _auth_context(token_id=None, scope=None):
            with pytest.raises(ForbiddenError):
                await check_vault_access(str(member_id), vault_name, required_role="writer")

    async def test_vault_owner_without_grant_is_403(self, pool):
        """The owner's own PAT exists (so this exercises the "token
        present but not on the allowlist" branch, complementing the
        no-token branch above) but was never granted — still denied.
        Proves ownership does not substitute for a grant on a marked
        vault (§5.1a: PAT-write-only)."""
        from app.exceptions import ForbiddenError
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import check_vault_access

        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")
        owner_token = await _create_token(pool, owner_id)  # never granted

        with _auth_context(token_id=owner_token, scope=None):
            with pytest.raises(ForbiddenError):
                await check_vault_access(str(owner_id), vault_name, required_role="writer")

    async def test_granted_pat_passes(self, pool):
        """A token on the allowlist passes regardless of the underlying
        user's ACL role — here a user with NO vault_access row and who
        is not the owner, proving the grant alone is sufficient (the
        "class-agnostic... collector, gardener, operator" allowlist
        design, not a re-derivation of ordinary ACL)."""
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import check_vault_access

        owner_id = await _create_user(pool)
        service_user_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")
        token_id = await _create_token(pool, service_user_id)
        await repo.add_grant(vault_id, token_id, "admin-user")

        with _auth_context(token_id=token_id, scope=None):
            result = await check_vault_access(
                str(service_user_id), vault_name, required_role="writer",
            )

        assert result["vault_id"] == vault_id

    async def test_action_limited_grant_passes_only_when_guard_declares_matching_action(self, pool):
        from app.exceptions import ForbiddenError
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import check_vault_access

        owner_id = await _create_user(pool)
        service_user_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "naut:upload", "admin-user")
        token_id = await _create_token(pool, service_user_id)
        await repo.add_grant(
            vault_id,
            token_id,
            "admin-user",
            write_actions=("file_upload",),
        )

        with _auth_context(token_id=token_id, scope=None):
            allowed = await check_vault_access(
                str(service_user_id),
                vault_name,
                required_role="writer",
                write_action="file_upload",
            )
            assert allowed["write_grant_actions"] == ["file_upload"]
            with pytest.raises(ForbiddenError):
                await check_vault_access(
                    str(service_user_id), vault_name, required_role="writer",
                )
            with pytest.raises(ForbiddenError):
                await check_vault_access(
                    str(service_user_id),
                    vault_name,
                    required_role="writer",
                    write_action="document_write",
                )

    async def test_delegated_human_requires_explicit_writer_membership(self, pool):
        from app.exceptions import ForbiddenError
        from app.services.access_service import check_delegated_vault_writer

        owner_id = await _create_user(pool)
        writer_id = await _create_user(pool)
        reader_id = await _create_user(pool)
        stranger_id = await _create_user(pool)
        service_writer_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        _other_vault_id, other_vault_name = await _create_named_vault(pool, owner_id)
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO vault_access (vault_id, user_id, role) VALUES ($1, $2, $3)",
                [
                    (vault_id, writer_id, "writer"),
                    (vault_id, reader_id, "reader"),
                    (vault_id, service_writer_id, "writer"),
                ],
            )
            await conn.execute(
                "UPDATE users SET account_kind = 'service' WHERE id = $1",
                service_writer_id,
            )
            await conn.execute(
                "INSERT INTO vault_access (vault_id, user_id, role) "
                "SELECT id, $1, 'writer' FROM vaults WHERE name = $2",
                stranger_id,
                other_vault_name,
            )

        assert (await check_delegated_vault_writer(str(owner_id), vault_name))["role"] == "owner"
        assert (await check_delegated_vault_writer(str(writer_id), vault_name))["role"] == "writer"
        with pytest.raises(ForbiddenError):
            await check_delegated_vault_writer(str(reader_id), vault_name)
        with pytest.raises(ForbiddenError):
            await check_delegated_vault_writer(str(stranger_id), vault_name)
        with pytest.raises(ForbiddenError, match="human"):
            await check_delegated_vault_writer(str(service_writer_id), vault_name)

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET account_status = 'suspended' WHERE id = $1",
                writer_id,
            )
        with pytest.raises(ForbiddenError):
            await check_delegated_vault_writer(str(writer_id), vault_name)

    async def test_unscoped_admin_bypasses_and_emits_audit(self, pool):
        """A3: a system admin with no PAT vault scope bypasses a marked
        vault's guard — but LOUDLY, via a `vault.write_policy_admin_bypass`
        event carrying managed_by/required_role/actor."""
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import check_vault_access

        owner_id = await _create_user(pool)
        admin_id = await _create_admin_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        with _auth_context(token_id=None, scope=None):
            result = await check_vault_access(str(admin_id), vault_name, required_role="writer")

        assert result["vault_id"] == vault_id

        async with pool.acquire() as conn:
            event = await conn.fetchrow(
                "SELECT * FROM events WHERE kind = 'vault.write_policy_admin_bypass' "
                "AND vault_id = $1",
                vault_id,
            )
        assert event is not None
        assert event["actor_id"] == str(admin_id)
        payload = (
            json.loads(event["payload"])
            if isinstance(event["payload"], str) else event["payload"]
        )
        assert payload["managed_by"] == "collector:acme"
        assert payload["required_role"] == "writer"

    async def test_scoped_admin_token_is_still_blocked(self, pool):
        """A *scoped* admin PAT must NOT get the A3 bypass — mirrors the
        Option B scope guard's own "a scope only ever subtracts" rule.
        The scope explicitly permits this vault so the earlier scope
        guard doesn't itself raise first; this isolates the write-policy
        guard's own admin-bypass branch."""
        from app.exceptions import ForbiddenError
        from app.models.vault_scope import VaultScope
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import check_vault_access

        owner_id = await _create_user(pool)
        admin_id = await _create_admin_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")
        admin_token = await _create_token(pool, admin_id)  # never granted

        scope = VaultScope(prefixes=(), extra_vaults=frozenset({vault_name}))

        with _auth_context(token_id=admin_token, scope=scope):
            with pytest.raises(ForbiddenError):
                await check_vault_access(str(admin_id), vault_name, required_role="writer")

    async def test_unmarked_vault_unaffected(self, pool):
        """No `vault_write_policy` row ⇒ the guard is a complete no-op:
        historical owner-bypass behaviour, unchanged."""
        from app.services.access_service import check_vault_access

        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        # No set_policy call — vault stays ungoverned.

        with _auth_context(token_id=None, scope=None):
            result = await check_vault_access(str(owner_id), vault_name, required_role="writer")

        assert result["vault_id"] == vault_id
        assert result["role"] == "owner"


class TestAkbSqlDualEnforcement:
    """Task 9 — `akb_sql`'s only `check_vault_access` call (the REST
    route in ``tables.py``) asks for ``required_role="reader"`` per
    referenced vault, so the mutating-role guard above never fires for
    it. ``table_service.execute_sql`` re-checks directly against the
    same allow-conditions, mirroring the pre-existing archived-vault
    dual block in the same function.
    """

    async def test_akb_sql_dual_enforcement_blocks_vt_write(self, pool, monkeypatch):
        from app.repositories import vault_write_policy_repo as repo
        from app.services import table_service
        from app.util.errors import VAULT_WRITE_MANAGED

        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        def _poison():
            raise AssertionError("must not reach the real executor — block happens before execution")

        monkeypatch.setattr(table_service, "get_user_sql_executor", _poison)

        with _auth_context(token_id=None, scope=None):
            result = await table_service.execute_sql(
                vault_names=[vault_name],
                user_id=str(owner_id),
                sql="UPDATE t SET x = 1",
                is_admin=False,
            )

        assert result["code"] == VAULT_WRITE_MANAGED

    async def test_akb_sql_dual_enforcement_admin_bypass(self, pool, monkeypatch):
        """Admin-bypass twin: an unscoped admin is NOT blocked (the SQL
        reaches the (stubbed) real executor) and the same loud audit
        event fires as the access_service guard's A3 branch."""
        from app.repositories import vault_write_policy_repo as repo
        from app.services import table_service

        owner_id = await _create_user(pool)
        admin_id = await _create_admin_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        calls = []

        class _StubExecutor:
            async def execute(self, **kwargs):
                calls.append(kwargs)
                return {"kind": "table_sql", "vaults": [], "result": "UPDATE 0"}

        monkeypatch.setattr(table_service, "get_user_sql_executor", lambda: _StubExecutor())

        with _auth_context(token_id=None, scope=None):  # unscoped admin
            result = await table_service.execute_sql(
                vault_names=[vault_name],
                user_id=str(admin_id),
                sql="UPDATE t SET x = 1",
                is_admin=True,
            )

        assert len(calls) == 1  # fell through to the (stubbed) real executor
        assert "code" not in result

        async with pool.acquire() as conn:
            event = await conn.fetchrow(
                "SELECT * FROM events WHERE kind = 'vault.write_policy_admin_bypass' "
                "AND vault_id = $1",
                vault_id,
            )
        assert event is not None

    async def test_akb_sql_multi_vault_blocks_on_the_second_referenced_vault(
        self, pool, monkeypatch,
    ):
        """The dual-enforcement loop must examine EVERY referenced vault,
        not just the first — an unmarked (harmless) vault listed before a
        marked-and-blocked one must not let the statement slip through."""
        from app.repositories import vault_write_policy_repo as repo
        from app.services import table_service
        from app.util.errors import VAULT_WRITE_MANAGED

        ok_owner_id = await _create_user(pool)
        _ok_vault_id, ok_vault_name = await _create_named_vault(pool, ok_owner_id)
        # ok_vault stays ungoverned — no set_policy call.

        blocked_owner_id = await _create_user(pool)
        blocked_vault_id, blocked_vault_name = await _create_named_vault(pool, blocked_owner_id)
        await repo.set_policy(blocked_vault_id, "collector:acme", "admin-user")

        def _poison():
            raise AssertionError("must not reach the real executor — blocked before execution")

        monkeypatch.setattr(table_service, "get_user_sql_executor", _poison)

        with _auth_context(token_id=None, scope=None):  # no PAT, not admin
            result = await table_service.execute_sql(
                vault_names=[ok_vault_name, blocked_vault_name],
                user_id=str(blocked_owner_id),
                sql="UPDATE t SET x = 1",
                is_admin=False,
            )

        assert result["code"] == VAULT_WRITE_MANAGED


class TestVaultInfoExposure:
    """Task 10 — `managed_by` on `get_vault_info` / `list_accessible_vaults`."""

    async def test_get_vault_info_managed_by_none_when_unmarked(self, pool):
        from app.services.access_service import get_vault_info

        owner_id = await _create_user(pool)
        _vault_id, vault_name = await _create_named_vault(pool, owner_id)

        info = await get_vault_info(str(owner_id), vault_name)

        assert info["managed_by"] is None

    async def test_get_vault_info_managed_by_set_when_marked(self, pool):
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import get_vault_info

        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        info = await get_vault_info(str(owner_id), vault_name)

        assert info["managed_by"] == "collector:acme"

    async def test_list_accessible_vaults_includes_managed_by(self, pool):
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import list_accessible_vaults

        owner_id = await _create_user(pool)
        marked_id, marked_name = await _create_named_vault(pool, owner_id)
        _plain_id, plain_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(marked_id, "gardener:distill", "admin-user")

        vaults = await list_accessible_vaults(str(owner_id))
        by_name = {v["name"]: v for v in vaults}

        assert by_name[marked_name]["managed_by"] == "gardener:distill"
        assert by_name[plain_name]["managed_by"] is None

    async def test_list_accessible_vaults_includes_managed_by_for_admin_viewer(self, pool):
        """The admin branch of `list_accessible_vaults` (sees every vault,
        not just owned/granted ones) has its own separate SQL — the JOIN
        must be duplicated there too, not just in the member branch."""
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import list_accessible_vaults

        owner_id = await _create_user(pool)
        admin_id = await _create_admin_user(pool)
        marked_id, marked_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(marked_id, "collector:acme", "admin-user")

        vaults = await list_accessible_vaults(str(admin_id))
        by_name = {v["name"]: v for v in vaults}

        assert by_name[marked_name]["managed_by"] == "collector:acme"


class TestAdminWritePolicyMarking:
    """Task 10 — the admin-only marking service functions
    (`access_service.set_vault_write_policy` / `remove_vault_write_policy`).

    Called directly: route-level `_require_admin` gating is covered by
    `test_vault_write_policy_routes_unit.py` (which mocks these functions
    out) — the service layer itself trusts the caller, the same convention
    every other admin-gated function in this module already uses (e.g.
    `account_service.suspend_user` takes `actor_id` for audit only).
    """

    async def test_set_vault_write_policy_marks_and_emits_event(self, pool):
        from app.services.access_service import get_vault_info, set_vault_write_policy

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        _vault_id, vault_name = await _create_named_vault(pool, owner_id)

        result = await set_vault_write_policy(
            str(admin_id), vault_name, "collector:acme", note="pilot vault",
        )

        assert result == {
            "vault": vault_name,
            "managed_by": "collector:acme",
            "note": "pilot vault",
            "marked": True,
        }
        info = await get_vault_info(str(owner_id), vault_name)
        assert info["managed_by"] == "collector:acme"

        async with pool.acquire() as conn:
            event = await conn.fetchrow(
                "SELECT * FROM events WHERE kind = 'vault.write_policy_changed' "
                "AND vault_id = (SELECT id FROM vaults WHERE name = $1)",
                vault_name,
            )
        assert event is not None
        assert event["actor_id"] == str(admin_id)
        payload = (
            json.loads(event["payload"]) if isinstance(event["payload"], str) else event["payload"]
        )
        assert payload == {
            "action": "marked",
            "vault": vault_name,
            "managed_by": "collector:acme",
            "note": "pilot vault",
        }

    async def test_set_vault_write_policy_rejects_missing_vault(self, pool):
        from app.exceptions import NotFoundError
        from app.services.access_service import set_vault_write_policy

        admin_id = await _create_admin_user(pool)

        with pytest.raises(NotFoundError):
            await set_vault_write_policy(str(admin_id), "vwp-does-not-exist", "collector:acme")

    async def test_set_vault_write_policy_rejects_empty_managed_by(self, pool):
        from app.exceptions import ValidationError
        from app.services.access_service import set_vault_write_policy

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        _vault_id, vault_name = await _create_named_vault(pool, owner_id)

        with pytest.raises(ValidationError):
            await set_vault_write_policy(str(admin_id), vault_name, "   ")

    async def test_set_vault_write_policy_rejects_external_git_vault(self, pool):
        """DECISION (task 10): marking an external-git mirror is REJECTED
        rather than silently accepted as harmless overlap — see
        `access_service.set_vault_write_policy`'s comment for the
        rationale (a grant could never make a write succeed there, since
        the mirror's own guard fires first and unconditionally; marking it
        anyway would misleadingly imply otherwise)."""
        from app.exceptions import ConflictError
        from app.services.access_service import set_vault_write_policy

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO vault_external_git (vault_id, remote_url) VALUES ($1, $2)",
                vault_id, "https://example.com/repo.git",
            )

        with pytest.raises(ConflictError):
            await set_vault_write_policy(str(admin_id), vault_name, "collector:acme")

    async def test_set_vault_write_policy_upsert_remarks_and_emits_again(self, pool):
        """Re-marking (upsert) audits AGAIN — every call is a real admin
        ACTION worth recording, not deduped against prior state. This is
        also what makes the unmark → write → re-mark break-glass sequence
        fully auditable (task 8/9's handoff note)."""
        from app.services.access_service import set_vault_write_policy

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        _vault_id, vault_name = await _create_named_vault(pool, owner_id)

        await set_vault_write_policy(str(admin_id), vault_name, "collector:v1")
        await set_vault_write_policy(str(admin_id), vault_name, "collector:v2", note="updated")

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE kind = 'vault.write_policy_changed' "
                "AND vault_id = (SELECT id FROM vaults WHERE name = $1)",
                vault_name,
            )
            managed_by = await conn.fetchval(
                "SELECT managed_by FROM vault_write_policy WHERE vault_id = "
                "(SELECT id FROM vaults WHERE name = $1)",
                vault_name,
            )
        assert count == 2
        assert managed_by == "collector:v2"

    async def test_remove_vault_write_policy_unmarks_and_emits_event(self, pool):
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import remove_vault_write_policy

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        result = await remove_vault_write_policy(str(admin_id), vault_name)

        assert result == {"vault": vault_name, "unmarked": True, "was_marked": True}
        assert await repo.get_policy(vault_id) is None

        async with pool.acquire() as conn:
            event = await conn.fetchrow(
                "SELECT * FROM events WHERE kind = 'vault.write_policy_changed' "
                "AND vault_id = $1 AND payload->>'action' = 'unmarked'",
                vault_id,
            )
        assert event is not None
        payload = (
            json.loads(event["payload"]) if isinstance(event["payload"], str) else event["payload"]
        )
        assert payload["managed_by"] == "collector:acme"  # what it WAS managed by

    async def test_remove_vault_write_policy_idempotent_on_unmarked_vault(self, pool):
        from app.services.access_service import remove_vault_write_policy

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        _vault_id, vault_name = await _create_named_vault(pool, owner_id)

        result = await remove_vault_write_policy(str(admin_id), vault_name)

        assert result == {"vault": vault_name, "unmarked": True, "was_marked": False}

    async def test_remove_vault_write_policy_rejects_missing_vault(self, pool):
        from app.exceptions import NotFoundError
        from app.services.access_service import remove_vault_write_policy

        admin_id = await _create_admin_user(pool)

        with pytest.raises(NotFoundError):
            await remove_vault_write_policy(str(admin_id), "vwp-does-not-exist")


class TestAdminWritePolicyGrants:
    """Task 10 — `add_vault_write_grant` / `remove_vault_write_grant`."""

    async def test_add_vault_write_grant_success_and_emits_event(self, pool):
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import add_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")
        token_id = await _create_token(pool, owner_id)

        result = await add_vault_write_grant(str(admin_id), vault_name, str(token_id))

        assert result == {"vault": vault_name, "token_id": str(token_id), "granted": True}
        assert await repo.is_granted(vault_id, token_id) is True

        async with pool.acquire() as conn:
            event = await conn.fetchrow(
                "SELECT * FROM events WHERE kind = 'vault.write_policy_changed' "
                "AND vault_id = $1 AND payload->>'action' = 'grant_added'",
                vault_id,
            )
        assert event is not None
        payload = (
            json.loads(event["payload"]) if isinstance(event["payload"], str) else event["payload"]
        )
        assert payload["token_id"] == str(token_id)
        assert payload["managed_by"] == "collector:acme"

    async def test_atomic_bootstrap_commits_policy_and_complete_grant_set(self, pool):
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import bootstrap_vault_write_policy

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        service_user_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        wildcard_id = await _create_token(pool, owner_id)
        upload_id = await _create_upload_service_token(
            pool, service_user_id, vault_name,
        )

        result = await bootstrap_vault_write_policy(
            str(admin_id),
            vault_name,
            "akb-platform:workspace-a",
            [
                {"token_id": str(wildcard_id), "write_actions": None},
                {
                    "token_id": str(upload_id),
                    "write_actions": ["file_upload"],
                },
            ],
            note="initial managed cutover",
        )

        assert result == {
            "vault": vault_name,
            "managed_by": "akb-platform:workspace-a",
            "note": "initial managed cutover",
            "marked": True,
            "grants": [
                {"token_id": str(wildcard_id), "write_actions": ["*"]},
                {"token_id": str(upload_id), "write_actions": ["file_upload"]},
            ],
        }
        assert await repo.get_policy(vault_id) is not None
        assert await repo.get_grant_actions(vault_id, wildcard_id) == frozenset({"*"})
        assert await repo.get_grant_actions(vault_id, upload_id) == frozenset(
            {"file_upload"}
        )

        async with pool.acquire() as conn:
            events = await conn.fetch(
                "SELECT payload FROM events "
                "WHERE kind = 'vault.write_policy_changed' AND vault_id = $1",
                vault_id,
            )
        assert len(events) == 1
        payload = (
            json.loads(events[0]["payload"])
            if isinstance(events[0]["payload"], str)
            else events[0]["payload"]
        )
        assert payload["action"] == "bootstrapped"
        assert payload["grants"] == result["grants"]

    async def test_atomic_bootstrap_rolls_back_policy_when_any_grant_fails(
        self, pool, monkeypatch,
    ):
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import bootstrap_vault_write_policy

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        first_id = await _create_token(pool, owner_id)
        second_id = await _create_token(pool, owner_id)
        real_add_grant = repo.add_grant
        calls = 0

        async def _fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected second-grant failure")
            await real_add_grant(*args, **kwargs)

        monkeypatch.setattr(repo, "add_grant", _fail_second)

        with pytest.raises(RuntimeError, match="second-grant failure"):
            await bootstrap_vault_write_policy(
                str(admin_id),
                vault_name,
                "akb-platform:workspace-a",
                [
                    {"token_id": str(first_id), "write_actions": None},
                    {"token_id": str(second_id), "write_actions": None},
                ],
            )

        assert await repo.get_policy(vault_id) is None
        async with pool.acquire() as conn:
            grant_count = await conn.fetchval(
                "SELECT COUNT(*) FROM vault_write_grants WHERE vault_id = $1",
                vault_id,
            )
            event_count = await conn.fetchval(
                "SELECT COUNT(*) FROM events "
                "WHERE kind = 'vault.write_policy_changed' AND vault_id = $1",
                vault_id,
            )
        assert grant_count == 0
        assert event_count == 0

    async def test_file_upload_grant_requires_exact_upload_service_profile(self, pool):
        from app.exceptions import ValidationError
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import add_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        service_user_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "naut:upload", "admin-user")
        token_id = await _create_upload_service_token(
            pool, service_user_id, vault_name,
        )

        result = await add_vault_write_grant(
            str(admin_id),
            vault_name,
            str(token_id),
            write_actions=["file_upload"],
        )

        assert result["write_actions"] == ["file_upload"]
        assert await repo.get_grant_actions(vault_id, token_id) == frozenset({"file_upload"})

        pat_id = await _create_token(pool, owner_id)
        with pytest.raises(ValidationError, match="service key"):
            await add_vault_write_grant(
                str(admin_id), vault_name, str(pat_id),
                write_actions=["file_upload"],
            )

        broad_user_id = await _create_user(pool)
        broad_id = await _create_upload_service_token(
            pool,
            broad_user_id,
            vault_name,
            scopes=("read", "write"),
        )
        with pytest.raises(ValidationError, match="coarse scope"):
            await add_vault_write_grant(
                str(admin_id), vault_name, str(broad_id),
                write_actions=["file_upload"],
            )

        unscoped_user_id = await _create_user(pool)
        unscoped_id = await _create_upload_service_token(
            pool,
            unscoped_user_id,
            vault_name,
            scoped_vault="",
        )
        with pytest.raises(ValidationError, match="exactly the target Vault"):
            await add_vault_write_grant(
                str(admin_id), vault_name, str(unscoped_id),
                write_actions=["file_upload"],
            )

        admin_service_user_id = await _create_user(pool)
        admin_service_id = await _create_upload_service_token(
            pool,
            admin_service_user_id,
            vault_name,
            is_admin=True,
        )
        with pytest.raises(ValidationError, match="non-admin service account"):
            await add_vault_write_grant(
                str(admin_id), vault_name, str(admin_service_id),
                write_actions=["file_upload"],
            )

        suspended_user_id = await _create_user(pool)
        suspended_id = await _create_upload_service_token(
            pool,
            suspended_user_id,
            vault_name,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET account_status = 'suspended' WHERE id = $1",
                suspended_user_id,
            )
        with pytest.raises(ValidationError, match="active service account"):
            await add_vault_write_grant(
                str(admin_id), vault_name, str(suspended_id),
                write_actions=["file_upload"],
            )

        expired_user_id = await _create_user(pool)
        expired_id = await _create_upload_service_token(
            pool,
            expired_user_id,
            vault_name,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE tokens SET expires_at = NOW() - INTERVAL '1 minute' WHERE id = $1",
                expired_id,
            )
        with pytest.raises(ValidationError, match="unexpired service key"):
            await add_vault_write_grant(
                str(admin_id), vault_name, str(expired_id),
                write_actions=["file_upload"],
            )

    async def test_add_vault_write_grant_rejects_unmarked_vault(self, pool):
        from app.exceptions import ConflictError
        from app.services.access_service import add_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        _vault_id, vault_name = await _create_named_vault(pool, owner_id)
        token_id = await _create_token(pool, owner_id)
        # vault stays ungoverned — no set_policy call

        with pytest.raises(ConflictError):
            await add_vault_write_grant(str(admin_id), vault_name, str(token_id))

    async def test_add_vault_write_grant_rejects_missing_token(self, pool):
        from app.exceptions import NotFoundError
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import add_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        with pytest.raises(NotFoundError):
            await add_vault_write_grant(str(admin_id), vault_name, str(uuid.uuid4()))

    async def test_add_vault_write_grant_rejects_missing_vault(self, pool):
        from app.exceptions import NotFoundError
        from app.services.access_service import add_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        token_id = await _create_token(pool, owner_id)

        with pytest.raises(NotFoundError):
            await add_vault_write_grant(str(admin_id), "vwp-does-not-exist", str(token_id))

    async def test_remove_vault_write_grant_success_and_emits_event(self, pool):
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import remove_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")
        token_id = await _create_token(pool, owner_id)
        await repo.add_grant(vault_id, token_id, "admin-user")

        result = await remove_vault_write_grant(str(admin_id), vault_name, str(token_id))

        assert result == {"vault": vault_name, "token_id": str(token_id), "revoked": True}
        assert await repo.is_granted(vault_id, token_id) is False

        async with pool.acquire() as conn:
            event = await conn.fetchrow(
                "SELECT * FROM events WHERE kind = 'vault.write_policy_changed' "
                "AND vault_id = $1 AND payload->>'action' = 'grant_removed'",
                vault_id,
            )
        assert event is not None

    async def test_remove_vault_write_grant_rejects_missing_token(self, pool):
        from app.exceptions import NotFoundError
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import remove_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        with pytest.raises(NotFoundError):
            await remove_vault_write_grant(str(admin_id), vault_name, str(uuid.uuid4()))

    async def test_remove_vault_write_grant_idempotent_when_not_granted(self, pool):
        from app.services.access_service import remove_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        from app.repositories import vault_write_policy_repo as repo
        await repo.set_policy(vault_id, "collector:acme", "admin-user")
        token_id = await _create_token(pool, owner_id)
        # never granted

        result = await remove_vault_write_grant(str(admin_id), vault_name, str(token_id))

        assert result == {"vault": vault_name, "token_id": str(token_id), "revoked": True}

    async def test_remove_vault_write_grant_rejects_missing_vault(self, pool):
        from app.exceptions import NotFoundError
        from app.services.access_service import remove_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        token_id = await _create_token(pool, owner_id)

        with pytest.raises(NotFoundError):
            await remove_vault_write_grant(str(admin_id), "vwp-does-not-exist", str(token_id))

    async def test_add_vault_write_grant_rejects_malformed_token_id(self, pool):
        """Characterization/pinning, not RED-driven — caught by self-review
        (code-review skill pass) as a real, reachable branch (an admin
        fat-fingering the token_id path segment) with zero prior coverage;
        the `try: uuid.UUID(token_id) except ...: raise ValidationError`
        guard already existed and was already correct, this just pins it
        so a future refactor can't silently let a raw ValueError leak out
        as a 500 instead of a clean 422."""
        from app.exceptions import ValidationError
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import add_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        with pytest.raises(ValidationError):
            await add_vault_write_grant(str(admin_id), vault_name, "not-a-uuid")

    async def test_remove_vault_write_grant_rejects_malformed_token_id(self, pool):
        """Same characterization rationale as the add-grant twin above."""
        from app.exceptions import ValidationError
        from app.repositories import vault_write_policy_repo as repo
        from app.services.access_service import remove_vault_write_grant

        admin_id = await _create_admin_user(pool)
        owner_id = await _create_user(pool)
        vault_id, vault_name = await _create_named_vault(pool, owner_id)
        await repo.set_policy(vault_id, "collector:acme", "admin-user")

        with pytest.raises(ValidationError):
            await remove_vault_write_grant(str(admin_id), vault_name, "not-a-uuid")
