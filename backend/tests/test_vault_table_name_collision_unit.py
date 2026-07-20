"""Regression tests for issue #285: cross-vault physical table-name fusion.

`pg_table_name` builds `vt_{sanitize(vault)}__{sanitize(table)}` and the
sanitiser maps `-` → `_`, so a hyphen RUN (or edge hyphen) in a vault name
forges the `__` separator: vault `a--b` + table `c` and vault `a` + table
`b__c` both map to `vt_a__b__c`. Covered here:

1. the tightened vault-name grammar (single hyphens only),
2. `create_vault` rejecting fusion-capable names before any side effect,
3. `create_table`'s physical-name preflight → precise 409 (DB-free),
4. the create/create race fallback on `DuplicateTableError` (DB-free),
5. genuine index-name clashes keeping their existing 422 message (DB-free),
6. slug producers never emitting edge hyphens after truncation,
7. the full fusion end-to-end against a real Postgres (skips without one).

DB-free tests reuse the fake-pool pattern from
`test_table_name_length_unit.py`.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.exceptions import ConflictError, ValidationError
from app.services import table_service
from app.services.agent_memory_service import sanitise_agent_id, sanitise_username
from app.services.document_service import DocumentService, validate_vault_name

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")

_COLS = [{"name": "x", "type": "text"}]


# ── 1. vault-name grammar ────────────────────────────────────────


@pytest.mark.parametrize("good", ["a", "a-b", "0", "a2-b3-c4", "prod-2026"])
async def test_vault_name_grammar_accepts_single_hyphen_names(good):
    validate_vault_name(good)  # must not raise


@pytest.mark.parametrize("bad", ["", "a--b", "a-", "-a", "a---b", "A", "a_b", "영업"])
async def test_vault_name_grammar_rejects_fusion_capable_names(bad):
    with pytest.raises(ValidationError):
        validate_vault_name(bad)


async def test_create_vault_validates_before_any_dependency():
    """A rejected name must be a pure 422 with zero side effects: a hollow
    instance (no repos, no git) proves validation runs first."""
    ds = object.__new__(DocumentService)  # bypass __init__ on purpose
    with pytest.raises(ValidationError):
        await ds.create_vault("a--b")
    with pytest.raises(ValidationError):
        await ds.create_vault("a-")


# ── DB-free create_table fixtures (pattern: test_table_name_length_unit) ──


class _AsyncCtx:
    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Carries create_table to the DDL stage: vault row exists, no
    registry duplicate, and `fetchval` answers the physical-name
    preflight with `taken`."""

    def __init__(self, vault_name: str, *, taken: bool):
        self._vault_name = vault_name
        self._taken = taken

    def transaction(self):
        return _AsyncCtx()

    async def fetchrow(self, sql: str, *params):
        if "FROM vaults" in sql:
            return {"name": self._vault_name}
        return None  # find_by_name → no same-vault duplicate

    async def fetchval(self, sql: str, *params):
        assert "to_regclass" in sql, f"unexpected fetchval: {sql}"
        return self._taken


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AsyncCtx(self._conn)


def _wire(monkeypatch, conn):
    async def _fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(table_service, "get_pool", _fake_get_pool)


# ── 3. preflight → precise 409 ───────────────────────────────────


async def test_fusion_preflight_raises_conflict_before_ddl(monkeypatch):
    _wire(monkeypatch, _FakeConn("a", taken=True))

    async def _must_not_run(*a, **k):
        raise AssertionError("create_dynamic_table must not run when the "
                             "physical name is already taken")

    monkeypatch.setattr(
        table_service.table_data_repo, "create_dynamic_table", _must_not_run)

    with pytest.raises(ConflictError) as ei:
        await table_service.create_table(uuid.uuid4(), "b__c", _COLS, actor_id="t")
    msg = str(ei.value)
    assert "vt_a__b__c" in msg and "another vault" in msg
    assert "unique-key or index" not in msg


# ── 4. create/create race fallback ───────────────────────────────


async def test_fusion_race_on_create_maps_to_same_conflict(monkeypatch):
    _wire(monkeypatch, _FakeConn("a", taken=False))  # preflight says free

    async def _lost_race(*a, **k):
        raise asyncpg.DuplicateTableError('relation "vt_a__b__c" already exists')

    monkeypatch.setattr(
        table_service.table_data_repo, "create_dynamic_table", _lost_race)

    with pytest.raises(ConflictError) as ei:
        await table_service.create_table(uuid.uuid4(), "b__c", _COLS, actor_id="t")
    assert "another vault" in str(ei.value)


# ── 5. genuine index-name clash keeps the legacy 422 ─────────────


async def test_index_name_clash_keeps_unique_key_message(monkeypatch):
    _wire(monkeypatch, _FakeConn("a", taken=False))

    async def _create_ok(*a, **k):
        return None

    async def _index_clash(*a, **k):
        raise asyncpg.DuplicateTableError('relation "my_custom_uk" already exists')

    monkeypatch.setattr(
        table_service.table_data_repo, "create_dynamic_table", _create_ok)
    monkeypatch.setattr(
        table_service.table_data_repo, "create_unique_constraint", _index_clash)

    with pytest.raises(ValidationError) as ei:
        await table_service.create_table(
            uuid.uuid4(), "b__c", _COLS, actor_id="t",
            unique_keys=[{"columns": ["x"], "name": "my_custom_uk"}],
        )
    assert "unique-key or index name" in str(ei.value)


# ── 6. slug producers can't mint edge-hyphen vault names ─────────


async def test_username_slug_never_ends_with_separator_after_cap():
    # 60th char lands right after a '-' so the cap used to re-expose it.
    raw = "a" * 59 + "-tail"
    slug = sanitise_username(raw)
    assert not slug.endswith(("-", ".")) and len(slug) <= 60
    validate_vault_name(f"agent-memory-{slug}"[:63])  # grammar-compatible

    agent = sanitise_agent_id("x" * 31 + "-tail")
    assert not agent.endswith(("-", "."))


# ── 7. end-to-end fusion against real PG (skips when unreachable) ─


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


async def test_cross_vault_fusion_end_to_end_pg():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_vtfusion_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    try:
        base, _ = _DSN.rsplit("/", 1)
        import app.db.postgres as pgmod
        from app.services import role_sync as role_sync_mod

        old_pool, old_rs = pgmod._pool, role_sync_mod._role_sync
        pool = await asyncpg.create_pool(dsn=f"{base}/{dbname}", min_size=1, max_size=4)
        pgmod._pool = pool
        try:
            await pgmod.init_db()  # real boot path: init.sql + migrations
            rs = role_sync_mod.RoleSync(pool)
            role_sync_mod.set_role_sync(rs)
            async with pool.acquire() as c:
                # Direct INSERT simulates a vault created before the
                # tightened grammar — create_vault would reject 'a--b' now.
                va = await c.fetchval(
                    "INSERT INTO vaults (name, git_path) VALUES ('a--b', '/tmp/f1') RETURNING id")
                vb = await c.fetchval(
                    "INSERT INTO vaults (name, git_path) VALUES ('a', '/tmp/f2') RETURNING id")
            await rs.on_vault_create(va)
            await rs.on_vault_create(vb)

            await table_service.create_table(va, "c", _COLS, actor_id="t")
            with pytest.raises(ConflictError) as ei:
                await table_service.create_table(vb, "b__c", _COLS, actor_id="t")
            msg = str(ei.value)
            assert "vt_a__b__c" in msg and "another vault" in msg
            assert "unique-key or index" not in msg

            async with pool.acquire() as c:
                leftovers = await c.fetchval(
                    "SELECT count(*) FROM vault_tables WHERE vault_id = $1", vb)
            assert leftovers == 0, "failed create must leave no registry row"
        finally:
            await pool.close()
            pgmod._pool = old_pool
            role_sync_mod._role_sync = old_rs
    finally:
        await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        await admin.close()
