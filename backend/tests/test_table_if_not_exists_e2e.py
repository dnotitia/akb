"""Live-PostgreSQL behaviour of `create_table(if_not_exists=...)`.

The unit suite proves the branching with fakes. Two properties cannot be
proven that way and are the reason this file exists:

  * The advisory lock actually SERIALIZES two concurrent creates of the same
    (vault, table). A fake cannot show that; only two real sessions
    contending on a real `pg_advisory_xact_lock` can.
  * The two post-check race windows really are closed. Before the lock, a
    loser that got past `find_by_name` could still be caught by the
    `to_regclass` preflight or lose the `CREATE TABLE` itself — and neither
    is recoverable, because by then the transaction is aborted.
    (`find_by_name` is the only window that should decide anything: seeing
    the row there is the intended `created=false`. The registry insert is
    not a third window for the same logical table — the loser's CREATE TABLE
    fails before it gets there.)

Skips when `AKB_TEST_DSN` is unset so a plain `pytest tests/` stays green.
If a DSN IS set and is unreachable the suite FAILS: a gate that green-skips
on a broken database is not a gate.

Isolation: a dedicated schema per run, dropped in teardown.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

DSN = os.getenv("AKB_TEST_DSN")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="AKB_TEST_DSN not set"),
]

_SCHEMA = "if_not_exists_e2e"


async def _conn():
    try:
        c = await asyncpg.connect(DSN)
    except (OSError, asyncpg.PostgresError) as e:  # pragma: no cover
        pytest.fail(f"AKB_TEST_DSN is set but unreachable: {e}")
    await c.execute(f"SET search_path TO {_SCHEMA}, public")
    return c


@pytest.fixture(autouse=True)
async def _schema():
    c = await asyncpg.connect(DSN)
    await c.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await c.execute(f"CREATE SCHEMA {_SCHEMA}")
    await c.close()
    yield
    c = await asyncpg.connect(DSN)
    await c.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await c.close()


# ── the lock itself ──────────────────────────────────────────────


async def test_advisory_lock_serializes_same_key():
    """Two sessions taking the same xact lock must not hold it at once.

    This is the property the whole design rests on: with it, the loser
    reaches `find_by_name` only AFTER the winner committed, so it sees the
    row and can report created=false — instead of racing ahead into a
    constraint violation it cannot recover from.
    """
    vault_id, name = uuid.uuid4(), "issues"
    key = f"{vault_id}:{name}"

    a, b = await _conn(), await _conn()
    order: list[str] = []
    try:
        tx_a = a.transaction()
        await tx_a.start()
        await a.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)
        order.append("a-acquired")

        async def _b():
            tx_b = b.transaction()
            await tx_b.start()
            await b.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)
            order.append("b-acquired")
            await tx_b.rollback()

        task = asyncio.create_task(_b())
        await asyncio.sleep(0.25)
        # B must still be blocked while A holds the lock.
        assert not task.done(), "second session acquired the lock concurrently"
        assert order == ["a-acquired"]

        await tx_a.rollback()          # release
        await asyncio.wait_for(task, timeout=5)
        assert order == ["a-acquired", "b-acquired"]
    finally:
        await a.close()
        await b.close()


async def test_different_keys_do_not_block_each_other():
    """The lock is per (vault, table) — unrelated creates must stay parallel,
    or every table create in the deployment serialises behind one mutex."""
    a, b = await _conn(), await _conn()
    try:
        tx_a = a.transaction()
        await tx_a.start()
        await a.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"{uuid.uuid4()}:alpha")

        tx_b = b.transaction()
        await tx_b.start()
        await asyncio.wait_for(
            b.fetchval("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                       f"{uuid.uuid4()}:beta"),
            timeout=3,
        )
        await tx_b.rollback()
        await tx_a.rollback()
    finally:
        await a.close()
        await b.close()


async def test_lock_is_released_by_rollback_not_just_commit():
    """`_xact_` scope: a failed create must not strand the lock, or one bad
    request wedges every subsequent create of that table name."""
    key = f"{uuid.uuid4()}:issues"
    a, b = await _conn(), await _conn()
    try:
        tx_a = a.transaction()
        await tx_a.start()
        await a.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)
        with pytest.raises(asyncpg.PostgresError):
            await a.execute("SELECT 1/0")     # abort the transaction
        await tx_a.rollback()

        tx_b = b.transaction()
        await tx_b.start()
        await asyncio.wait_for(
            b.fetchval("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                       key),
            timeout=3,
        )
        await tx_b.rollback()
    finally:
        await a.close()
        await b.close()


# ── the reason the original design was not implementable ─────────


async def test_reread_after_constraint_violation_is_impossible():
    """Pins the fact that motivated the lock.

    The first design caught `UniqueViolationError` and re-read the winner's
    row in place. This proves that cannot work: after the violation the
    transaction is aborted and every subsequent query on that connection
    fails until rollback. If this test ever starts passing a plain SELECT,
    the constraint that forced the advisory-lock design has changed.
    """
    c = await _conn()
    try:
        await c.execute(
            f"CREATE TABLE {_SCHEMA}.t (id int PRIMARY KEY)")
        tx = c.transaction()
        await tx.start()
        await c.execute(f"INSERT INTO {_SCHEMA}.t VALUES (1)")
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute(f"INSERT INTO {_SCHEMA}.t VALUES (1)")

        with pytest.raises(asyncpg.InFailedSQLTransactionError):
            await c.fetchval(f"SELECT count(*) FROM {_SCHEMA}.t")
        await tx.rollback()
    finally:
        await c.close()


async def test_create_table_concurrency_through_the_service():
    """The load-bearing guarantee, exercised through `create_table` itself
    rather than through lock primitives.

    Two concurrent `if_not_exists=True` creates of the SAME (vault, table)
    must both succeed with exactly one `created=true`, exactly one registry
    row, exactly one physical table and exactly one `table.create` event.
    Before the advisory lock the loser raced past `find_by_name` into the
    `to_regclass` preflight or the `CREATE TABLE` itself and surfaced a
    different unrecoverable exception from each.
    """
    from urllib.parse import urlparse

    from app.config import settings
    from app.db import postgres as pg_mod
    from app.services import table_service

    # `create_table` acquires the APP pool (settings.asyncpg_dsn), not the
    # test DSN, and the configured host is a container-internal name. Point
    # the pool at the test database for the duration of this test.
    saved_db = (settings.db_user, settings.db_password, settings.db_host,
                settings.db_port, settings.db_name)
    orig_role_sync = table_service.get_role_sync

    vault_id = uuid.uuid4()
    vault_name = "cvt"
    table_name = "issues"
    pg_name = f"vt_{vault_name}__{table_name}"
    cols = [{"name": "title", "type": "text"}]
    setup = None

    # The try is ARMED BEFORE the first mutation, not after: `close_pool()`
    # can itself fail or be cancelled, which would otherwise strand the
    # mutated settings. Everything that can raise — including both
    # `pytest.skip` sites — is inside, so a skipped run restores too.
    try:
        u = urlparse(DSN)
        settings.db_user, settings.db_password = u.username, u.password
        settings.db_host, settings.db_port = u.hostname, u.port or 5432
        settings.db_name = (u.path or "/").lstrip("/")
        await pg_mod.close_pool()

        setup = await asyncpg.connect(DSN)
        if not await setup.fetchval(
            "SELECT to_regclass('public.vaults') IS NOT NULL"
        ):
            # The CI live-PG job runs against a FRESH database on purpose, so
            # this must bootstrap rather than skip. A skip here would make the
            # feature's load-bearing guarantee a gate that never fires — which
            # is exactly what happened before this file was added to the job.
            from app.db.postgres import init_db
            await init_db()
        await setup.execute(
            "INSERT INTO vaults (id, name, description, git_path, created_at) "
            "VALUES ($1, $2, '', $3, NOW())",
            vault_id, vault_name, f"/tmp/{vault_name}",
        )

        # RoleSync is wired in the app lifespan; the create only calls
        # grant_table_in_conn on the winning path, and per-vault PG roles are
        # not what this test is about.
        class _RoleSyncStub:
            async def grant_table_in_conn(self, *a, **k):
                return None

        table_service.get_role_sync = lambda: _RoleSyncStub()

        async def _one():
            return await table_service.create_table(
                vault_id, table_name, cols, actor_id="t",
                if_not_exists=True, can_read_existing=True,
            )

        results = await asyncio.gather(_one(), _one(), return_exceptions=True)

        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, f"a concurrent create raised: {errors!r}"

        created = [r["created"] for r in results]
        assert created.count(True) == 1, (
            f"exactly one caller must have created the table, got {created}")
        assert created.count(False) == 1

        assert await setup.fetchval(
            "SELECT count(*) FROM vault_tables WHERE vault_id=$1 AND name=$2",
            vault_id, table_name,
        ) == 1, "more than one registry row"
        assert await setup.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"public.{pg_name}"
        ), "physical table missing"
        assert await setup.fetchval(
            "SELECT count(*) FROM events WHERE kind='table.create' "
            "AND vault_id=$1", vault_id,
        ) == 1, "a no-op emitted a table.create event"
    finally:
        # Process-global state is restored FIRST and unconditionally: a
        # failure while cleaning up rows must not leave the next test
        # pointed at this database.
        table_service.get_role_sync = orig_role_sync
        (settings.db_user, settings.db_password, settings.db_host,
         settings.db_port, settings.db_name) = saved_db
        try:
            if setup is not None:
                try:
                    await setup.execute(
                        f"DROP TABLE IF EXISTS public.{pg_name} CASCADE")
                    await setup.execute(
                        "DELETE FROM vault_tables WHERE vault_id=$1", vault_id)
                    await setup.execute(
                        "DELETE FROM events WHERE vault_id=$1", vault_id)
                    await setup.execute(
                        "DELETE FROM vaults WHERE id=$1", vault_id)
                finally:
                    # A failed DELETE must not leak the connection.
                    await setup.close()
        finally:
            await pg_mod.close_pool()


async def test_hashtextextended_key_is_stable_and_distinguishes_names():
    """The lock key is a hash, so collisions are possible in principle —
    two different (vault, table) pairs sharing a lock would only cost
    throughput, never correctness. What must hold is determinism: the same
    key always maps to the same lock."""
    c = await _conn()
    try:
        vault = uuid.uuid4()
        k1 = await c.fetchval(
            "SELECT hashtextextended($1, 0)", f"{vault}:issues")
        k2 = await c.fetchval(
            "SELECT hashtextextended($1, 0)", f"{vault}:issues")
        k3 = await c.fetchval(
            "SELECT hashtextextended($1, 0)", f"{vault}:incidents")
        assert k1 == k2
        assert k1 != k3
    finally:
        await c.close()
