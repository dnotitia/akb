"""PostgreSQL proof that a Vault's event ids follow commit order.

The SSE event tail reads ``events`` with ``id > cursor`` in id order and moves
its cursor to the last id it read (``events_repo.list_vault_events``). If a
transaction drew a smaller id but committed after a transaction with a larger
id, a reader that already moved past the larger id never read the smaller one.
Migration 132 serializes event inserts per Vault until commit, so the id order
within a Vault is the commit order. These tests drive two real transactions in
the problematic interleaving and follow the cursor the way the tail does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import uuid
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import asyncpg
import pytest

from app.repositories import table_data_repo
from app.repositories.events_repo import emit_event, list_vault_events

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_MIGRATION = "132_events_commit_order.py"
# How long a second writer gets to finish while the first one is still open.
# A blocked writer stays blocked until the first commits, so this only bounds
# how long the test waits before it looks.
_BLOCK_PROBE_SECONDS = 0.5
_UNBLOCKED_SECONDS = 10.0


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except OSError, asyncpg.PostgresError:
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


@contextlib.asynccontextmanager
async def _fresh_database() -> AsyncIterator[asyncpg.Pool]:
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    admin = await asyncpg.connect(_DSN)
    name = f"akb_events_order_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=24)
    from app.db import postgres as postgres_module

    previous_pool = postgres_module._pool
    try:
        init_sql = (Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql").read_text()
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
        postgres_module._pool = pool
        await postgres_module._apply_migrations()
        yield pool
    finally:
        postgres_module._pool = previous_pool
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _make_vault(pool: asyncpg.Pool, name: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            name,
            f"/tmp/{name}.git",
        )


async def _emit(conn: asyncpg.Connection, vault_id: uuid.UUID | None, marker: str) -> int:
    return await emit_event(
        conn, "document.put", vault_id=vault_id, payload={"marker": marker},
    )


async def _follow(
    reader: asyncpg.Connection,
    vault_id: uuid.UUID,
    cursor: int,
    delivered: list[int],
) -> int:
    """Read everything visible after ``cursor`` the way the SSE tail does."""
    while True:
        rows = await list_vault_events(reader, vault_id, after_id=cursor, limit=100)
        if not rows:
            return cursor
        for row in rows:
            delivered.append(int(row["id"]))
            cursor = int(row["id"])


async def _write_and_commit(
    conn: asyncpg.Connection,
    write: Callable[[asyncpg.Connection], Awaitable[int]],
) -> int:
    async with conn.transaction():
        return await write(conn)


class _OpenTransaction:
    """A transaction the test commits explicitly; it rolls back otherwise."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._transaction = conn.transaction()
        self._finished = False

    async def __aenter__(self) -> _OpenTransaction:
        await self._transaction.start()
        return self

    async def commit(self) -> None:
        await self._transaction.commit()
        self._finished = True

    async def __aexit__(self, *_exc: object) -> None:
        if not self._finished:
            await self._transaction.rollback()


async def _next_sequence_value(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow("SELECT last_value, is_called FROM events_id_seq")
    return int(row["last_value"]) + (1 if row["is_called"] else 0)


async def _late_commit_delivery(
    pool: asyncpg.Pool,
    vault_id: uuid.UUID,
    first: Callable[[asyncpg.Connection], Awaitable[int]],
    second: Callable[[asyncpg.Connection], Awaitable[int]],
) -> tuple[list[int], int, int, bool]:
    """Open ``first``, let ``second`` try to commit, read, then commit ``first``.

    Returns what a cursor-following reader delivered, both ids, and whether the
    second writer was still waiting while the first was open.
    """
    async with (
        pool.acquire() as writer_a,
        pool.acquire() as writer_b,
        pool.acquire() as reader,
    ):
        delivered: list[int] = []
        cursor = 0
        second_task: asyncio.Task[int] | None = None
        try:
            async with _OpenTransaction(writer_a) as first_transaction:
                first_id = await first(writer_a)
                second_task = asyncio.create_task(_write_and_commit(writer_b, second))
                done, _ = await asyncio.wait({second_task}, timeout=_BLOCK_PROBE_SECONDS)
                second_was_waiting = not done
                # A tail polling now sees whatever has committed so far.
                cursor = await _follow(reader, vault_id, cursor, delivered)
                await first_transaction.commit()
            second_id = await asyncio.wait_for(second_task, timeout=_UNBLOCKED_SECONDS)
            cursor = await _follow(reader, vault_id, cursor, delivered)
        finally:
            if second_task is not None and not second_task.done():
                second_task.cancel()
                with contextlib.suppress(BaseException):
                    await second_task
        return delivered, first_id, second_id, second_was_waiting


async def test_cursor_reader_receives_an_event_that_commits_after_a_later_one():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "order-late-commit")

        delivered, first_id, second_id, second_was_waiting = await _late_commit_delivery(
            pool,
            vault_id,
            lambda conn: _emit(conn, vault_id, "first"),
            lambda conn: _emit(conn, vault_id, "second"),
        )

        assert sorted(delivered) == sorted([first_id, second_id]), (
            f"a committed event was never delivered: delivered={delivered}, "
            f"first={first_id} (inserted first), second={second_id}"
        )
        assert delivered == [first_id, second_id]
        assert first_id < second_id
        assert second_was_waiting, "the second writer did not wait for the first to commit"


@pytest.mark.parametrize("first_writer,second_writer", [("table", "emit"), ("emit", "table")])
async def test_dynamic_table_trigger_events_follow_commit_order(first_writer, second_writer):
    async with _fresh_database() as pool:
        vault_name = "order-rows-changed"
        vault_id = await _make_vault(pool, vault_name)
        pg_name = table_data_repo.pg_table_name(vault_name, "items")
        async with pool.acquire() as conn:
            await table_data_repo.create_dynamic_table(
                conn,
                pg_name,
                [{"name": "value", "type": "text"}],
                vault_name=vault_name,
                vault_id=vault_id,
                resource_uri=f"akb://{vault_name}/table/items",
            )

        async def table_write(conn: asyncpg.Connection) -> int:
            await conn.execute(f"INSERT INTO {pg_name} (value) VALUES ('row')")
            return await conn.fetchval(
                "SELECT max(id) FROM events WHERE vault_id = $1 AND kind = 'table.rows_changed'",
                vault_id,
            )

        writers = {
            "table": table_write,
            "emit": lambda conn: _emit(conn, vault_id, "emit"),
        }
        delivered, first_id, second_id, second_was_waiting = await _late_commit_delivery(
            pool, vault_id, writers[first_writer], writers[second_writer],
        )

        assert delivered == [first_id, second_id]
        assert second_was_waiting
        async with pool.acquire() as conn:
            kinds = [
                row["kind"]
                for row in await conn.fetch(
                    "SELECT kind FROM events WHERE vault_id = $1 ORDER BY id", vault_id,
                )
            ]
        expected = {"table": "table.rows_changed", "emit": "document.put"}
        assert kinds == [expected[first_writer], expected[second_writer]]


async def test_restricted_sql_role_row_write_follows_commit_order():
    """User SQL runs under a role that cannot touch `events` or its sequence.

    Its row-change event is inserted by the SECURITY DEFINER trigger function,
    so the ordering trigger draws the id with the definer's privileges.
    """
    role = f"akb_events_order_{uuid.uuid4().hex[:8]}"
    try:
        async with _fresh_database() as pool:
            vault_name = "order-restricted"
            vault_id = await _make_vault(pool, vault_name)
            pg_name = table_data_repo.pg_table_name(vault_name, "items")
            async with pool.acquire() as conn:
                await table_data_repo.create_dynamic_table(
                    conn,
                    pg_name,
                    [{"name": "value", "type": "text"}],
                    vault_name=vault_name,
                    vault_id=vault_id,
                    resource_uri=f"akb://{vault_name}/table/items",
                )
                await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
                await conn.execute(f'GRANT INSERT ON {pg_name} TO "{role}"')

            async with pool.acquire() as conn:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute(f'SET LOCAL ROLE "{role}"')
                        await conn.execute(
                            "INSERT INTO events (vault_id, kind) VALUES ($1, 'forged')", vault_id,
                        )

            async def restricted_write(conn: asyncpg.Connection) -> int:
                await conn.execute(f'SET LOCAL ROLE "{role}"')
                await conn.execute(f"INSERT INTO {pg_name} (value) VALUES ('row')")
                await conn.execute("RESET ROLE")
                return await conn.fetchval(
                    "SELECT max(id) FROM events WHERE vault_id = $1 AND kind = 'table.rows_changed'",
                    vault_id,
                )

            delivered, first_id, second_id, second_was_waiting = await _late_commit_delivery(
                pool,
                vault_id,
                restricted_write,
                lambda conn: _emit(conn, vault_id, "after-restricted"),
            )
            assert delivered == [first_id, second_id]
            assert second_was_waiting
            async with pool.acquire() as conn:
                await conn.execute(f'DROP OWNED BY "{role}"')
    finally:
        # Roles are cluster-wide and outlive the dropped test database.
        with contextlib.suppress(OSError, asyncpg.PostgresError):
            admin = await asyncpg.connect(_DSN, timeout=2)
            try:
                await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            finally:
                await admin.close()


async def test_events_of_different_vaults_do_not_wait_for_each_other():
    async with _fresh_database() as pool:
        first_vault = await _make_vault(pool, "order-vault-a")
        second_vault = await _make_vault(pool, "order-vault-b")
        async with pool.acquire() as writer_a, pool.acquire() as writer_b:
            async with _OpenTransaction(writer_a):
                await _emit(writer_a, first_vault, "a")
                second_id = await asyncio.wait_for(
                    _write_and_commit(writer_b, lambda conn: _emit(conn, second_vault, "b")),
                    timeout=_UNBLOCKED_SECONDS,
                )
        assert second_id > 0


async def test_events_without_a_vault_take_no_vault_lock():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "order-null-vault")
        async with pool.acquire() as writer_a, pool.acquire() as writer_b:
            # A Vault event's lock does not hold back an account event ...
            async with _OpenTransaction(writer_a):
                await _emit(writer_a, vault_id, "vault")
                await asyncio.wait_for(
                    _write_and_commit(writer_b, lambda conn: _emit(conn, None, "account")),
                    timeout=_UNBLOCKED_SECONDS,
                )
            # ... and open account events do not hold back each other.
            async with _OpenTransaction(writer_a):
                await _emit(writer_a, None, "account-open")
                await asyncio.wait_for(
                    _write_and_commit(writer_b, lambda conn: _emit(conn, None, "account-next")),
                    timeout=_UNBLOCKED_SECONDS,
                )


async def test_returned_stored_and_notified_ids_are_the_trigger_assigned_id():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "order-returning")
        notifications: asyncio.Queue[str] = asyncio.Queue()

        def on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
            notifications.put_nowait(payload)

        async with pool.acquire() as listener, pool.acquire() as writer:
            await listener.add_listener("akb_events", on_notify)
            try:
                # Column defaults are evaluated before BEFORE triggers: the
                # default draws one value and the trigger keeps a second one.
                next_value = await _next_sequence_value(writer)
                event_id = await _write_and_commit(
                    writer, lambda conn: _emit(conn, vault_id, "returning"),
                )
                assert event_id == next_value + 1
                assert await writer.fetchval("SELECT last_value FROM events_id_seq") == event_id
                stored = await writer.fetchval(
                    "SELECT id FROM events WHERE payload->>'marker' = 'returning'",
                )
                assert stored == event_id
                notified = await asyncio.wait_for(notifications.get(), timeout=_UNBLOCKED_SECONDS)
                assert notified.rsplit(":", 1)[-1] == str(event_id)

                # An event without a Vault keeps its default id.
                next_value = await _next_sequence_value(writer)
                account_id = await _write_and_commit(
                    writer, lambda conn: _emit(conn, None, "account"),
                )
                assert account_id == next_value
            finally:
                await listener.remove_listener("akb_events", on_notify)


async def test_concurrent_writers_never_leave_a_cursor_behind_a_late_commit():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "order-concurrent")
        rng = random.Random(406)
        delays = [rng.uniform(0.0, 0.03) for _ in range(16)]
        committed: list[int] = []
        delivered: list[int] = []
        stop = asyncio.Event()

        async def writer(delay: float) -> None:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    event_id = await _emit(conn, vault_id, f"writer-{delay}")
                    await asyncio.sleep(delay)
                committed.append(event_id)

        async def reader() -> None:
            cursor = 0
            async with pool.acquire() as conn:
                while not stop.is_set():
                    cursor = await _follow(conn, vault_id, cursor, delivered)
                    await asyncio.sleep(0.002)
                await _follow(conn, vault_id, cursor, delivered)

        reading = asyncio.create_task(reader())
        await asyncio.gather(*(writer(delay) for delay in delays))
        stop.set()
        await reading

        assert len(committed) == len(delays)
        assert sorted(delivered) == sorted(committed), (
            f"committed events never delivered: {sorted(set(committed) - set(delivered))}"
        )


async def test_migration_upgrades_a_database_with_existing_events():
    async with _fresh_database() as pool:
        from app.db import postgres as postgres_module

        vault_id = await _make_vault(pool, "order-upgrade")
        async with pool.acquire() as conn:
            # Return the database to its shape before this migration.
            await conn.execute("DROP TRIGGER akb_events_commit_order_trigger ON events")
            await conn.execute("DROP FUNCTION akb_events_commit_order()")
            await conn.execute("DELETE FROM schema_migrations WHERE filename = $1", _MIGRATION)
            notify_before = await conn.fetchval(
                "SELECT pg_get_functiondef('akb_events_notify'::regproc)",
            )
            for marker in ("old-1", "old-2"):
                await _emit(conn, vault_id, marker)
            await _emit(conn, None, "old-account")
            existing = [
                dict(row)
                for row in await conn.fetch(
                    "SELECT id, vault_id, payload FROM events ORDER BY id",
                )
            ]
            # A cached sequence would let one session draw below another;
            # the migration must reset it. No session has drawn since, so no
            # backend holds a cached block when the migration runs.
            await conn.execute("ALTER SEQUENCE events_id_seq CACHE 5")

        await postgres_module._apply_migrations()

        async with pool.acquire() as conn:
            assert [
                dict(row)
                for row in await conn.fetch(
                    "SELECT id, vault_id, payload FROM events ORDER BY id",
                )
            ] == existing
            assert await conn.fetchval(
                "SELECT cache_size FROM pg_sequences WHERE sequencename = 'events_id_seq'",
            ) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'events'::regclass "
                "AND tgname = 'akb_events_commit_order_trigger'",
            ) == 1
            assert await conn.fetchval(
                "SELECT pg_get_functiondef('akb_events_notify'::regproc)",
            ) == notify_before
            assert await conn.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE filename = $1", _MIGRATION,
            ) == 1

            # Running it again changes nothing.
            module = postgres_module._load_migration(_MIGRATION)
            await module.migrate(conn)
            assert await conn.fetchval(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'events'::regclass "
                "AND tgname = 'akb_events_commit_order_trigger'",
            ) == 1

        async with pool.acquire() as conn:
            new_id = await _emit(conn, vault_id, "new")
            assert new_id > max(row["id"] for row in existing)
            payload = json.loads(
                await conn.fetchval("SELECT payload::text FROM events WHERE id = $1", new_id),
            )
            assert payload == {"marker": "new"}

        delivered, first_id, second_id, second_was_waiting = await _late_commit_delivery(
            pool,
            vault_id,
            lambda conn: _emit(conn, vault_id, "after-first"),
            lambda conn: _emit(conn, vault_id, "after-second"),
        )
        old_vault_ids = [row["id"] for row in existing if row["vault_id"] == vault_id]
        assert delivered == [*old_vault_ids, new_id, first_id, second_id]
        assert second_was_waiting
