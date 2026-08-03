"""State-matrix and safety tests for migration 050 (drop `todos`).

050 is destructive — it archives rows to `todos_archive` and then drops the
source — so its behaviour is pinned against a real PostgreSQL rather than a
mock. Covered:

  * the bug being fixed actually reproduces on the pre-change schema, and stops
    reproducing after the migration (the investigation inferred it from
    schema + code + data but never executed it);
  * every entry in the state matrix — neither table, source only, archive only,
    both present;
  * the archive keeps the rows but NOT the `NOT NULL` / FK coupling to `users`
    that caused the bug;
  * both-present fails CLOSED rather than dropping a source whose rows the
    pre-existing archive may not contain;
  * an unexpected dependent (a view) makes the drop fail loudly instead of
    being cascaded away;
  * re-running is a clean no-op.

Talks to a real Postgres via `AKB_TEST_DSN` (default the audit stack's
`localhost:5433`); skips when unreachable so the suite runs unattended. Runs in
a disposable database so it never touches a dev DB's data. Registered in the
`pgvector e2e (live DB)` CI job, which sets `AKB_TEST_DSN` — the DB-free unit
job would otherwise skip it in both jobs, making it a gate that never fires.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")

# The pre-change schema, verbatim from `main`'s init.sql:442-458 — the NOT NULL
# on the two user columns is the whole point.
_PRE_CHANGE_TODOS = """
CREATE TABLE todos (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    assignee_id UUID NOT NULL REFERENCES users(id),
    created_by UUID NOT NULL REFERENCES users(id),
    vault_id UUID REFERENCES vaults(id),
    title TEXT NOT NULL,
    note TEXT,
    priority TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
)
"""

_MINIMAL_DEPS = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username TEXT NOT NULL UNIQUE
);
CREATE TABLE vaults (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    owner_id UUID REFERENCES users(id)
);
CREATE TABLE vault_access (
    vault_id UUID, user_id UUID, granted_by UUID REFERENCES users(id)
);
CREATE TABLE publications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_by UUID REFERENCES users(id)
);
"""


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _load_migration():
    """Load 050 from source (NOT import_module, which could honour a stale
    __pycache__ .pyc and mask a source regression)."""
    path = (
        Path(__file__).resolve().parents[1]
        / "app" / "db" / "migrations" / "050_drop_todos.py"
    )
    spec = importlib.util.spec_from_file_location("mig_050_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Fixture:
    """A disposable database with the minimal pre-change schema."""

    def __init__(self, admin, conn, dbname):
        self.admin, self.conn, self.dbname = admin, conn, dbname


async def _make_db():
    admin = await asyncpg.connect(_DSN)
    dbname = f"akb_mig050_{uuid.uuid4().hex[:8]}"
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    base, _ = _DSN.rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/{dbname}")
    await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    await conn.execute(_MINIMAL_DEPS)
    return _Fixture(admin, conn, dbname)


async def _drop_db(fx: _Fixture):
    await fx.conn.close()
    await fx.admin.execute(f'DROP DATABASE "{fx.dbname}" WITH (FORCE)')
    await fx.admin.close()


async def _seed_blocked_user(conn) -> str:
    """A user holding a todos row that no vault cascade would clear."""
    uid = await conn.fetchval(
        "INSERT INTO users (username) VALUES ($1) RETURNING id", f"u{uuid.uuid4().hex[:8]}"
    )
    await conn.execute(
        "INSERT INTO todos (assignee_id, created_by, vault_id, title) "
        "VALUES ($1, $1, NULL, 'orphan')",
        uid,
    )
    return uid


async def _old_delete_user_account(conn, uid):
    """`delete_user_account` as it stood before this change — statement for
    statement, and deliberately WITHOUT a transaction wrapper, which is what
    let the first two updates commit while the account survived."""
    await conn.execute("UPDATE vault_access SET granted_by = NULL WHERE granted_by = $1", uid)
    await conn.execute("UPDATE publications SET created_by = NULL WHERE created_by = $1", uid)
    await conn.execute("UPDATE todos SET assignee_id = NULL WHERE assignee_id = $1", uid)
    await conn.execute("UPDATE todos SET created_by = NULL WHERE created_by = $1", uid)
    await conn.execute("DELETE FROM users WHERE id = $1", uid)


async def _new_delete_user_account(conn, uid):
    """The same block after the `todos` writes were removed."""
    await conn.execute("UPDATE vault_access SET granted_by = NULL WHERE granted_by = $1", uid)
    await conn.execute("UPDATE publications SET created_by = NULL WHERE created_by = $1", uid)
    await conn.execute("DELETE FROM users WHERE id = $1", uid)


@pytest.mark.asyncio
async def test_migration_050_unblocks_account_deletion():
    """The regression this migration exists for, end to end."""
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    fx = await _make_db()
    try:
        conn = fx.conn
        await conn.execute(_PRE_CHANGE_TODOS)
        uid = await _seed_blocked_user(conn)

        # BEFORE: the NOT NULL write aborts the sequence and the account lives.
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await _old_delete_user_account(conn, uid)
        assert await conn.fetchval("SELECT count(*) FROM users WHERE id = $1", uid) == 1, (
            "DELETE FROM users must NOT have run — that is the bug"
        )
        # And it is permanent, not transient: retrying hits the same wall.
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await _old_delete_user_account(conn, uid)

        await _load_migration().migrate(conn=conn)

        # AFTER: the same account deletes cleanly.
        await _new_delete_user_account(conn, uid)
        assert await conn.fetchval("SELECT count(*) FROM users WHERE id = $1", uid) == 0
    finally:
        await _drop_db(fx)


@pytest.mark.asyncio
async def test_migration_050_archive_preserves_rows_without_the_faulty_coupling():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    fx = await _make_db()
    try:
        conn = fx.conn
        await conn.execute(_PRE_CHANGE_TODOS)
        for _ in range(3):
            await _seed_blocked_user(conn)
        before = await conn.fetchval("SELECT count(*) FROM todos")

        await _load_migration().migrate(conn=conn)

        assert await conn.fetchval("SELECT to_regclass('public.todos')") is None
        assert await conn.fetchval("SELECT count(*) FROM todos_archive") == before

        nullability = {
            r["column_name"]: r["is_nullable"]
            for r in await conn.fetch(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'todos_archive'"
            )
        }
        # The archive must NOT reacquire the constraint that caused the bug…
        assert nullability["assignee_id"] == "YES"
        assert nullability["created_by"] == "YES"
        # …nor any FK, so nothing can cascade into it and user deletion is free.
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'todos_archive'::regclass AND contype = 'f'"
        ) == 0
    finally:
        await _drop_db(fx)


@pytest.mark.asyncio
async def test_migration_050_state_matrix():
    """Neither table / source only / archive only — all safe and idempotent."""
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    mig = _load_migration()

    # (a) neither table — fresh install, must no-op.
    fx = await _make_db()
    try:
        await mig.migrate(conn=fx.conn)
        assert await fx.conn.fetchval("SELECT to_regclass('public.todos')") is None
        assert await fx.conn.fetchval("SELECT to_regclass('public.todos_archive')") is None
    finally:
        await _drop_db(fx)

    # (b) source only — the normal path, then a re-run that must not disturb it.
    fx = await _make_db()
    try:
        await fx.conn.execute(_PRE_CHANGE_TODOS)
        await _seed_blocked_user(fx.conn)
        await mig.migrate(conn=fx.conn)
        archived = await fx.conn.fetchval("SELECT count(*) FROM todos_archive")
        await mig.migrate(conn=fx.conn)  # no-op
        assert await fx.conn.fetchval("SELECT count(*) FROM todos_archive") == archived
        assert await fx.conn.fetchval("SELECT to_regclass('public.todos')") is None
    finally:
        await _drop_db(fx)

    # (c) archive only — already migrated; must not recreate or touch anything.
    fx = await _make_db()
    try:
        await fx.conn.execute("CREATE TABLE todos_archive (id uuid, title text)")
        await fx.conn.execute("INSERT INTO todos_archive VALUES (uuid_generate_v4(), 'kept')")
        await mig.migrate(conn=fx.conn)
        assert await fx.conn.fetchval("SELECT count(*) FROM todos_archive") == 1
        assert await fx.conn.fetchval("SELECT to_regclass('public.todos')") is None
    finally:
        await _drop_db(fx)


@pytest.mark.asyncio
async def test_migration_050_fails_closed_when_both_tables_exist():
    """A pre-existing archive is NOT evidence the rows are already saved.

    This migration commits the archive and the drop together, so it can never
    leave that state itself. Both present means the archive came from
    somewhere else and may not contain what `todos` currently holds — dropping
    the source would silently lose rows.
    """
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    fx = await _make_db()
    try:
        conn = fx.conn
        await conn.execute(_PRE_CHANGE_TODOS)
        await _seed_blocked_user(conn)
        # An unrelated / stale archive that does NOT hold the live row.
        await conn.execute("CREATE TABLE todos_archive (id uuid, title text)")

        with pytest.raises(RuntimeError, match="both `todos` and `todos_archive` exist"):
            await _load_migration().migrate(conn=conn)

        # The source survives untouched — nothing was discarded.
        assert await conn.fetchval("SELECT count(*) FROM todos") == 1
        assert await conn.fetchval("SELECT count(*) FROM todos_archive") == 0
    finally:
        await _drop_db(fx)


@pytest.mark.asyncio
async def test_migration_050_refuses_to_cascade_away_an_unexpected_dependent():
    """A site-local view on `todos` must break the drop, not vanish silently."""
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    fx = await _make_db()
    try:
        conn = fx.conn
        await conn.execute(_PRE_CHANGE_TODOS)
        await _seed_blocked_user(conn)
        await conn.execute("CREATE VIEW ops_open_todos AS SELECT id, title FROM todos")

        with pytest.raises(asyncpg.exceptions.DependentObjectsStillExistError):
            await _load_migration().migrate(conn=conn)

        # Transactional: the aborted run left neither a half-archive nor a drop.
        assert await conn.fetchval("SELECT to_regclass('public.todos')") is not None
        assert await conn.fetchval("SELECT to_regclass('public.todos_archive')") is None
    finally:
        await _drop_db(fx)
