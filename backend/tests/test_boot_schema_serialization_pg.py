"""Two pods booting at once must not run schema DDL concurrently.

`_apply_migrations` had taken an advisory lock since it was written, and its
comment names the hazard: the `chunks` ALTERs take an ACCESS EXCLUSIVE lock
that races live workers during a rolling deploy. `init.sql` — which creates
and alters the same tables — ran outside that lock, on the request pool.

Two pods doing that at the same time deadlocked on the `chunks`/`documents`
pair. Observed on a rolling deploy: one pod exited 3 at startup with
`DeadlockDetectedError`, and only the restart recovered it. It happened once in
five deploys, so a test that just boots twice and hopes proves nothing.

This asserts the property instead of the symptom: while another session holds
the lock, a boot lands **no DDL at all**. That is a statement about database
state rather than about timing, so it does not depend on how long anything
takes.

Runs in a disposable database; registered in the `pgvector e2e (live DB)` job.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@asynccontextmanager
async def _empty_database():
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_boot_lock_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    base, _ = _DSN.rsplit("/", 1)
    try:
        yield f"{base}/{name}"
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _tables_exist(dsn: str) -> bool:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval("SELECT to_regclass('public.vaults') IS NOT NULL")
    finally:
        await conn.close()


async def test_a_boot_lands_no_ddl_while_another_session_holds_the_lock(monkeypatch):
    from app.db import postgres as pg

    # The lock, not the migration list, is what this test is about; running 100+
    # migrations would only make it slow and give the assertions nothing extra.
    async def _no_migrations(conn, applied):
        return None

    monkeypatch.setattr(pg, "_apply_pending_migrations", _no_migrations)

    async with _empty_database() as dsn:
        assert not await _tables_exist(dsn), "the fixture database should start empty"

        holder = await asyncpg.connect(dsn)
        booting = await asyncpg.connect(dsn)
        try:
            await holder.execute(
                "SELECT pg_advisory_lock($1)", pg._MIGRATION_LOCK_KEY,
            )
            boot = asyncio.create_task(
                pg._run_boot_schema(booting, init_sql=_INIT_SQL),
            )
            # Long enough that an unlocked boot would have created tables —
            # `init.sql` on an empty database takes well under a second.
            await asyncio.sleep(2)
            assert not boot.done(), "the boot did not wait for the lock"
            assert not await _tables_exist(dsn), (
                "a boot created tables while another session held the schema "
                "lock — the DDL is running concurrently across pods"
            )

            await holder.execute(
                "SELECT pg_advisory_unlock($1)", pg._MIGRATION_LOCK_KEY,
            )
            await asyncio.wait_for(boot, timeout=60)
            assert await _tables_exist(dsn), "the boot never applied init.sql"
        finally:
            await booting.close()
            await holder.close()


async def test_the_lock_is_released_so_the_next_boot_proceeds(monkeypatch):
    """A held-forever lock would turn every later pod into a startup hang."""
    from app.db import postgres as pg

    async def _no_migrations(conn, applied):
        return None

    monkeypatch.setattr(pg, "_apply_pending_migrations", _no_migrations)

    async with _empty_database() as dsn:
        first = await asyncpg.connect(dsn)
        try:
            await pg._run_boot_schema(first, init_sql=_INIT_SQL)
        finally:
            await first.close()

        # A fresh session must be able to take the lock immediately.
        second = await asyncpg.connect(dsn)
        try:
            got = await second.fetchval(
                "SELECT pg_try_advisory_lock($1)", pg._MIGRATION_LOCK_KEY,
            )
            assert got, "the boot did not release the schema lock"
            await second.execute(
                "SELECT pg_advisory_unlock($1)", pg._MIGRATION_LOCK_KEY,
            )
        finally:
            await second.close()
