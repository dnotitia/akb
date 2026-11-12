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
import time
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
# A writer that nothing holds back finishes well within this.
_BLOCK_PROBE_SECONDS = 0.5
_UNBLOCKED_SECONDS = 10.0
# The longest a transaction waits for its Vault's event lock (the deadline in
# `akb_lock_vault_events`, migration 132).
_EVENT_LOCK_WAIT_SECONDS = 5.0


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


async def _undo_migration_132(conn: asyncpg.Connection) -> None:
    """Return the database to its shape before migration 132."""
    await conn.execute("DROP TRIGGER IF EXISTS akb_events_commit_order_trigger ON events")
    await conn.execute("DROP FUNCTION IF EXISTS akb_events_commit_order()")
    await conn.execute("DROP TABLE IF EXISTS vault_event_order")
    await conn.execute("DROP TABLE IF EXISTS events_row_change_queue")
    await conn.execute("DROP FUNCTION IF EXISTS akb_events_row_change_flush()")
    from app.db import postgres as postgres_module

    # Migration 086's row-change function, as it was before 132 replaced it.
    await postgres_module._load_migration("086_dynamic_table_rows_changed.py").migrate(conn)
    await conn.execute(
        "ALTER TABLE events ALTER COLUMN id SET DEFAULT nextval('events_id_seq'::regclass)",
    )
    await conn.execute("DELETE FROM schema_migrations WHERE filename = $1", _MIGRATION)


async def _next_sequence_value(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow("SELECT last_value, is_called FROM events_id_seq")
    return int(row["last_value"]) + (1 if row["is_called"] else 0)


async def _waits_for(
    observer: asyncpg.Connection,
    task: asyncio.Task,
    waiter: asyncpg.Connection,
    holder: asyncpg.Connection,
) -> bool:
    """True once ``waiter`` waits for ``holder``; False if ``task`` ends first.

    A waiter is either blocked on a lock ``holder`` holds, or sleeping in the
    Vault lock's poll (`akb_lock_vault_events`), which takes no lock-manager
    wait and is the only sleep in these sessions.
    """
    while not task.done():
        state = await observer.fetchrow(
            "SELECT $2 = ANY(pg_blocking_pids($1)) AS blocked, "
            "coalesce(wait_event = 'PgSleep', false) AS polling "
            "FROM pg_stat_activity WHERE pid = $1",
            waiter.get_server_pid(),
            holder.get_server_pid(),
        )
        if state is not None and (state["blocked"] or state["polling"]):
            return True
        await asyncio.sleep(0.01)
    return False


async def _late_commit_delivery(
    pool: asyncpg.Pool,
    vault_id: uuid.UUID,
    first: Callable[[asyncpg.Connection], Awaitable[object]],
    second: Callable[[asyncpg.Connection], Awaitable[object]],
) -> tuple[list[int], list[tuple[int, str, str | None]], bool]:
    """Open ``first``, let ``second`` try to commit, read, then commit ``first``.

    Returns what a cursor-following reader delivered, the Vault's events the
    two writers committed as ``(id, kind, marker)`` in id order, and whether
    PostgreSQL reported the second writer blocked by the first.
    """
    async with (
        pool.acquire() as writer_a,
        pool.acquire() as writer_b,
        pool.acquire() as reader,
        pool.acquire() as observer,
    ):
        baseline = await reader.fetchval(
            "SELECT coalesce(max(id), 0) FROM events WHERE vault_id = $1", vault_id,
        )
        delivered: list[int] = []
        cursor = baseline
        second_task: asyncio.Task | None = None
        try:
            async with _OpenTransaction(writer_a) as first_transaction:
                await first(writer_a)
                second_task = asyncio.create_task(_write_and_commit(writer_b, second))
                second_was_waiting = await asyncio.wait_for(
                    _waits_for(observer, second_task, writer_b, writer_a),
                    timeout=_UNBLOCKED_SECONDS,
                )
                # A tail polling now sees whatever has committed so far.
                cursor = await _follow(reader, vault_id, cursor, delivered)
                await first_transaction.commit()
            await asyncio.wait_for(second_task, timeout=_UNBLOCKED_SECONDS)
            cursor = await _follow(reader, vault_id, cursor, delivered)
        finally:
            if second_task is not None and not second_task.done():
                second_task.cancel()
                with contextlib.suppress(BaseException):
                    await second_task
        committed = [
            (row["id"], row["kind"], row["marker"])
            for row in await reader.fetch(
                "SELECT id, kind, payload->>'marker' AS marker FROM events "
                "WHERE vault_id = $1 AND id > $2 ORDER BY id",
                vault_id,
                baseline,
            )
        ]
        return delivered, committed, second_was_waiting


async def test_cursor_reader_receives_an_event_that_commits_after_a_later_one():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "order-late-commit")

        delivered, committed, second_was_waiting = await _late_commit_delivery(
            pool,
            vault_id,
            lambda conn: _emit(conn, vault_id, "first"),
            lambda conn: _emit(conn, vault_id, "second"),
        )

        assert delivered == [event_id for event_id, _, _ in committed], (
            f"a committed event was never delivered: delivered={delivered}, committed={committed}"
        )
        assert [marker for _, _, marker in committed] == ["first", "second"]
        assert second_was_waiting, "the second writer did not wait for the first to commit"


@pytest.mark.parametrize("first_writer,second_writer", [("table", "emit"), ("emit", "table")])
async def test_dynamic_table_trigger_events_follow_commit_order(first_writer, second_writer):
    """A row-change event is inserted when its transaction commits, so it
    takes the Vault's turn at commit; either way the tail misses nothing."""
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

        async def table_write(conn: asyncpg.Connection) -> None:
            await conn.execute(f"INSERT INTO {pg_name} (value) VALUES ('row')")

        writers = {
            "table": table_write,
            "emit": lambda conn: _emit(conn, vault_id, "emit"),
        }
        delivered, committed, second_was_waiting = await _late_commit_delivery(
            pool, vault_id, writers[first_writer], writers[second_writer],
        )

        assert delivered == [event_id for event_id, _, _ in committed]
        kinds = {"table": "table.rows_changed", "emit": "document.put"}
        # An open table write has not inserted its event yet, so the emitter
        # goes first; an open emitter makes the table write's commit wait.
        assert second_was_waiting is (first_writer == "emit")
        assert [kind for _, kind, _ in committed] == (
            [kinds[first_writer], kinds[second_writer]]
            if second_was_waiting
            else [kinds[second_writer], kinds[first_writer]]
        )


async def test_restricted_sql_role_row_write_follows_commit_order():
    """User SQL runs under a role that cannot touch `events` or its sequence.

    Its row-change event is inserted at commit, still under that role, by the
    SECURITY DEFINER queue function, so the ordering trigger runs with the
    definer's privileges.
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

            async def restricted_write(conn: asyncpg.Connection) -> None:
                # The role stays in effect until commit, as in `akb_sql`.
                await conn.execute(f'SET LOCAL ROLE "{role}"')
                await conn.execute(f"INSERT INTO {pg_name} (value) VALUES ('row')")

            delivered, committed, _ = await _late_commit_delivery(
                pool,
                vault_id,
                lambda conn: _emit(conn, vault_id, "first"),
                restricted_write,
            )
            assert delivered == [event_id for event_id, _, _ in committed]
            assert [kind for _, kind, _ in committed] == ["document.put", "table.rows_changed"]
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


async def test_a_sql_role_cannot_hold_a_vault_event_lock():
    """Any role may take any advisory lock, so the ordering lock is a row lock
    on a table no SQL role is granted. A role holding the key an advisory
    ordering lock would use stalls nothing, and the lock row is out of reach."""
    role = f"akb_events_order_{uuid.uuid4().hex[:8]}"
    try:
        async with _fresh_database() as pool:
            vault_id = await _make_vault(pool, "order-held-key")
            async with pool.acquire() as conn:
                await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
            async with pool.acquire() as holder, pool.acquire() as writer:
                async with _OpenTransaction(holder):
                    await holder.execute(f'SET LOCAL ROLE "{role}"')
                    await holder.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('akb-events:' || $1, 0))",
                        str(vault_id),
                    )
                    started = time.monotonic()
                    await asyncio.wait_for(
                        _write_and_commit(writer, lambda conn: _emit(conn, vault_id, "while-key-held")),
                        timeout=_BLOCK_PROBE_SECONDS * 4,
                    )
                    waited = time.monotonic() - started
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        async with holder.transaction():
                            await holder.execute("SELECT 1 FROM vault_event_order FOR UPDATE")
            assert waited < _BLOCK_PROBE_SECONDS
    finally:
        with contextlib.suppress(OSError, asyncpg.PostgresError):
            admin = await asyncpg.connect(_DSN, timeout=2)
            try:
                await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            finally:
                await admin.close()


async def test_waiting_for_a_busy_vault_is_bounded():
    """A Vault held past the limit fails the waiting insert with 55P03 instead
    of keeping its pooled connection until the statement timeout."""
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "order-bounded")
        async with pool.acquire() as holder, pool.acquire() as waiter:
            async with _OpenTransaction(holder):
                await _emit(holder, vault_id, "holder")
                started = time.monotonic()
                with pytest.raises(asyncpg.LockNotAvailableError):
                    await asyncio.wait_for(
                        _write_and_commit(waiter, lambda conn: _emit(conn, vault_id, "waiter")),
                        timeout=_EVENT_LOCK_WAIT_SECONDS + 3,
                    )
                waited = time.monotonic() - started
            # The failed insert left nothing behind and the Vault is usable.
            assert await _write_and_commit(waiter, lambda conn: _emit(conn, vault_id, "after")) > 0
        assert _EVENT_LOCK_WAIT_SECONDS - 0.5 <= waited < _EVENT_LOCK_WAIT_SECONDS + 2
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM events WHERE vault_id = $1 AND payload->>'marker' = 'waiter'",
                vault_id,
            ) == 0


async def test_a_waiter_behind_another_waiter_gives_up_at_the_same_deadline():
    """The bound is one deadline per insert. A per-lock timeout would let a
    waiter queued behind another waiter wait once for that waiter and again for
    the holder."""
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "order-deadline")
        # The Vault's lock row exists, as it does after its first event; the
        # waiters then queue on the row itself, the second behind the first.
        async with pool.acquire() as conn:
            await _write_and_commit(conn, lambda c: _emit(c, vault_id, "earlier"))

        async def wait(conn: asyncpg.Connection, marker: str) -> float:
            started = time.monotonic()
            with pytest.raises(asyncpg.LockNotAvailableError):
                await _write_and_commit(conn, lambda c: _emit(c, vault_id, marker))
            return time.monotonic() - started

        async with (
            pool.acquire() as holder,
            pool.acquire() as first,
            pool.acquire() as second,
        ):
            async with _OpenTransaction(holder):
                await _emit(holder, vault_id, "holder")
                first_wait = asyncio.create_task(wait(first, "first"))
                await asyncio.sleep(0.2)
                second_wait = await asyncio.wait_for(
                    wait(second, "second"), timeout=_EVENT_LOCK_WAIT_SECONDS * 3,
                )
                first_waited = await asyncio.wait_for(first_wait, timeout=_UNBLOCKED_SECONDS)
        assert first_waited <= _EVENT_LOCK_WAIT_SECONDS + 0.5
        assert second_wait <= _EVENT_LOCK_WAIT_SECONDS + 0.5, (
            f"the second waiter waited {second_wait:.2f}s"
        )


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
                # The trigger is the only source of ids: one value per event.
                next_value = await _next_sequence_value(writer)
                event_id = await _write_and_commit(
                    writer, lambda conn: _emit(conn, vault_id, "returning"),
                )
                assert event_id == next_value
                assert await writer.fetchval("SELECT last_value FROM events_id_seq") == event_id
                stored = await writer.fetchval(
                    "SELECT id FROM events WHERE payload->>'marker' = 'returning'",
                )
                assert stored == event_id
                notified = await asyncio.wait_for(notifications.get(), timeout=_UNBLOCKED_SECONDS)
                assert notified.rsplit(":", 1)[-1] == str(event_id)

                # So does an event without a Vault, and an INSERT naming no id
                # (as the previous release's code does) still gets one.
                async with writer.transaction():
                    unnamed = await writer.fetchval(
                        "INSERT INTO events (vault_id, kind) VALUES ($1, 'document.put') RETURNING id",
                        vault_id,
                    )
                assert unnamed == await writer.fetchval("SELECT last_value FROM events_id_seq")
                assert await writer.fetchval(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'events' AND column_name = 'id'",
                ) is None
                assert await writer.fetchval(
                    "SELECT pg_get_serial_sequence('public.events', 'id')",
                ) == "public.events_id_seq"
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
            await _undo_migration_132(conn)
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
            assert await conn.fetchval(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'events' AND column_name = 'id'",
            ) is None

            # Dynamic tables' row changes now go through the commit-time queue.
            assert "events_row_change_queue" in await conn.fetchval(
                "SELECT pg_get_functiondef('akb_dynamic_table_rows_changed'::regproc)",
            )

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

        delivered, committed, second_was_waiting = await _late_commit_delivery(
            pool,
            vault_id,
            lambda conn: _emit(conn, vault_id, "after-first"),
            lambda conn: _emit(conn, vault_id, "after-second"),
        )
        assert delivered == [event_id for event_id, _, _ in committed]
        assert [marker for _, _, marker in committed] == ["after-first", "after-second"]
        assert second_was_waiting


async def test_ids_a_session_cached_before_the_migration_are_not_drawn_after_it():
    """With a sequence cache above 1 a session reserves a block of ids at its
    first draw, and could later draw an id below one another session drew in
    between. The migration sets the cache to 1. PostgreSQL then gives the
    sequence new storage, and a session that still holds a block drops it at
    its next draw instead of using it."""
    async with _fresh_database() as pool:
        from app.db import postgres as postgres_module

        vault_id = await _make_vault(pool, "order-cached-block")
        async with pool.acquire() as conn:
            await _undo_migration_132(conn)
            await conn.execute("ALTER SEQUENCE events_id_seq CACHE 20")
        async with pool.acquire() as early, pool.acquire() as late:
            # `early` draws before the migration, as a previous-release
            # process does, and keeps the rest of its block.
            before = await _emit(early, vault_id, "early, before the migration")
            await postgres_module._apply_migrations()
            after = await _emit(late, vault_id, "late, after the migration")
            again = await _emit(early, vault_id, "early, after the migration")
        assert before < after < again


async def _blocked_by(observer: asyncpg.Connection, waiter: asyncpg.Connection, holder: asyncpg.Connection) -> None:
    async def poll() -> None:
        while not await observer.fetchval(
            "SELECT $2 = ANY(pg_blocking_pids($1))", waiter.get_server_pid(), holder.get_server_pid(),
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=_UNBLOCKED_SECONDS)


async def test_migration_steps_aside_while_a_long_transaction_writes_events():
    """The migration's lock request must not queue other Vaults' inserts."""
    async with _fresh_database() as pool:
        from app.db import postgres as postgres_module

        busy_vault = await _make_vault(pool, "order-migrate-busy")
        other_vault = await _make_vault(pool, "order-migrate-other")
        async with pool.acquire() as conn:
            await _undo_migration_132(conn)
        module = postgres_module._load_migration(_MIGRATION)
        worst = 0.0
        async with (
            pool.acquire() as long_writer,
            pool.acquire() as other,
            pool.acquire() as migrator,
            pool.acquire() as observer,
        ):
            migration: asyncio.Task | None = None
            try:
                async with _OpenTransaction(long_writer):
                    await long_writer.execute(
                        "INSERT INTO events (vault_id, kind) VALUES ($1, 'long')", busy_vault,
                    )
                    migration = asyncio.create_task(
                        postgres_module._run_one_migration(migrator, _MIGRATION, module),
                    )
                    await _blocked_by(observer, migrator, long_writer)
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        started = time.monotonic()
                        async with other.transaction():
                            await other.execute(
                                "INSERT INTO events (vault_id, kind) VALUES ($1, 'other')", other_vault,
                            )
                        worst = max(worst, time.monotonic() - started)
                        await asyncio.sleep(0.05)
                    assert not migration.done()
                # The long transaction ended; the next attempt gets the lock.
                await asyncio.wait_for(migration, timeout=_UNBLOCKED_SECONDS)
            finally:
                if migration is not None and not migration.done():
                    migration.cancel()
                    with contextlib.suppress(BaseException):
                        await migration

        assert worst < 1.0, f"another Vault's insert waited {worst:.2f}s behind the migration"
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'events'::regclass "
                "AND tgname = 'akb_events_commit_order_trigger'",
            ) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE filename = $1", _MIGRATION,
            ) == 1


async def test_migration_gives_up_clearly_while_events_keep_being_written(monkeypatch):
    async with _fresh_database() as pool:
        from app.db import postgres as postgres_module

        vault_id = await _make_vault(pool, "order-migrate-bound")
        async with pool.acquire() as conn:
            await _undo_migration_132(conn)
        module = postgres_module._load_migration(_MIGRATION)
        monkeypatch.setattr(module, "ATTEMPTS", 3)
        monkeypatch.setattr(module, "FIRST_BACKOFF_SECONDS", 0.05)
        async with pool.acquire() as long_writer, pool.acquire() as migrator:
            async with _OpenTransaction(long_writer):
                await long_writer.execute(
                    "INSERT INTO events (vault_id, kind) VALUES ($1, 'long')", vault_id,
                )
                with pytest.raises(RuntimeError, match="could not lock the events table in 3 attempts"):
                    await asyncio.wait_for(module.migrate(migrator), timeout=_UNBLOCKED_SECONDS)
            # Nothing of the migration stayed behind.
            assert await migrator.fetchval(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'events'::regclass "
                "AND tgname = 'akb_events_commit_order_trigger'",
            ) == 0
