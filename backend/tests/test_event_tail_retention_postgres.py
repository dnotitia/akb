"""PostgreSQL proof that an event tail gap means deleted events, not a low cursor.

The tail refused a cursor more than one below the Vault's earliest retained
event. Event ids are shared by every Vault, so a cursor taken on an empty Vault,
before its first event, is usually far below that event without having missed
anything, and the tail refused it all the same. A trigger on events now records,
per Vault, the highest event id deleted, in every statement that deletes, and
the tail refuses only a cursor below that.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import asyncpg
import pytest

from app.db.postgres import _load_migration, _run_one_migration
from app.services.event_tail_service import EventCursorCodec, EventGapError
from tests.test_event_tail_budget_postgres import _CURSOR_SECRET, _changes, _make_reader, _positions, _tail
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _make_vault, _sweep


pytestmark = pytest.mark.asyncio

_MIGRATION = "133_event_retention_watermark.py"
_BACKFILL = "134_event_retention_backfill.py"
# The sweep an earlier release runs: it knows nothing of the watermark.
_EARLIER_SWEEP = """
    WITH d AS (
        DELETE FROM events
         WHERE redis_published_at IS NOT NULL
           AND redis_published_at < NOW() - INTERVAL '7 days'
        RETURNING 1
    )
    SELECT COUNT(*) FROM d
"""


async def _as_before_the_migration(pool: asyncpg.Pool) -> None:
    """Remove what migrations 133 and 134 installed and set, keeping their tables."""
    await pool.execute("DROP TRIGGER IF EXISTS akb_events_record_deletions ON events")
    await pool.execute("DROP TRIGGER IF EXISTS akb_vaults_drop_retention_marks ON vaults")
    await pool.execute("DELETE FROM event_retention_watermarks")
    await pool.execute("DELETE FROM event_retention_backfill")


async def _install_and_backfill(conn: asyncpg.Connection) -> None:
    """Apply migration 133 and then 134, each committing on its own, as a boot does."""
    for filename in (_MIGRATION, _BACKFILL):
        migration = _load_migration(filename)
        assert migration is not None
        async with conn.transaction():
            await migration.migrate(conn=conn)


async def _trigger_installed(pool: asyncpg.Pool) -> bool:
    return bool(
        await pool.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'events'::regclass AND tgname = 'akb_events_record_deletions'"
        )
    )


async def _publish_long_ago(pool: asyncpg.Pool, event_ids: list[int]) -> None:
    """Mark events as published past the sweeper's grace period."""
    await pool.execute(
        "UPDATE events SET redis_published_at = NOW() - INTERVAL '8 days' WHERE id = ANY($1::bigint[])",
        event_ids,
    )


async def _watermarks(pool: asyncpg.Pool) -> dict[uuid.UUID, int]:
    rows = await pool.fetch("SELECT vault_id, deleted_through FROM event_retention_watermarks")
    return {row["vault_id"]: row["deleted_through"] for row in rows}


async def _write_event(conn: asyncpg.Connection, vault_id: uuid.UUID) -> int:
    return await conn.fetchval(
        """
        INSERT INTO events (vault_id, kind, resource_uri, actor_id, payload)
        VALUES ($1, 'document.put', 'akb://event-test/document.put', 'alice', '{}'::jsonb)
        RETURNING id
        """,
        vault_id,
    )


async def _waits_for(pool: asyncpg.Pool, waiter: asyncpg.Connection, holder: asyncpg.Connection) -> None:
    """Return once ``waiter`` waits for a lock that ``holder`` holds."""

    async def poll() -> None:
        while not await pool.fetchval(
            "SELECT $2 = ANY(pg_blocking_pids($1))", waiter.get_server_pid(), holder.get_server_pid()
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


async def test_a_cursor_taken_before_a_vaults_first_event_is_not_refused(monkeypatch):
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "retention-first-event")
        other_vault_id = await _make_vault(setup_pool, "retention-other")
        user_id = await _make_reader(setup_pool, "retention-reader", [vault_id])
        # A cursor taken while the Vault was empty.
        cursor = EventCursorCodec(_CURSOR_SECRET).encode(vault_id, (), 0)
        # Another Vault's events take the next hundred ids before this one's first.
        for _ in range(100):
            await _insert_event(setup_pool, other_vault_id, "document.put", "alice")
        event_id = await _insert_event(setup_pool, vault_id, "document.put", "alice")
        async with _tail(monkeypatch, setup_pool) as tail:
            resumed = await tail.open(user_id=user_id, vault="retention-first-event", last_event_id=cursor)
            try:
                delivered = await asyncio.wait_for(_changes(resumed, 1), 5)
            finally:
                await resumed.aclose()

        assert event_id == 101
        assert _positions(delivered) == [event_id]


async def test_a_cursor_older_than_swept_events_is_refused_at_the_watermark(monkeypatch):
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "retention-swept")
        user_id = await _make_reader(setup_pool, "retention-swept-reader", [vault_id])
        event_ids = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(5)]
        await _publish_long_ago(setup_pool, event_ids[:3])
        assert await _sweep(monkeypatch, setup_pool) == 3
        codec = EventCursorCodec(_CURSOR_SECRET)
        async with _tail(monkeypatch, setup_pool) as tail:
            # A cursor that never received the third event, which is gone.
            with pytest.raises(EventGapError) as exc_info:
                await tail.open(
                    user_id=user_id,
                    vault="retention-swept",
                    last_event_id=codec.encode(vault_id, (), event_ids[0]),
                )
            details = exc_info.value.details
            assert codec.decode(details["earliest_cursor"]) == (vault_id, (), event_ids[2])
            assert codec.decode(details["latest_cursor"]) == (vault_id, (), event_ids[4])

            # Resuming at the earliest cursor, and replaying from the earliest, both work.
            for opened in (
                {"last_event_id": details["earliest_cursor"]},
                {"start": "earliest"},
            ):
                body = await tail.open(user_id=user_id, vault="retention-swept", **opened)
                try:
                    delivered = await asyncio.wait_for(_changes(body, 2), 5)
                finally:
                    await body.aclose()
                assert _positions(delivered) == event_ids[3:]


async def test_a_delete_records_the_watermark_in_its_own_statement(monkeypatch):
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-sweeper")
        first = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(2)]
        await _publish_long_ago(setup_pool, first)
        assert await _sweep(monkeypatch, setup_pool) == 2
        assert await _watermarks(setup_pool) == {vault_id: first[-1]}

        later = [await _insert_event(setup_pool, vault_id, "document.update", "alice") for _ in range(2)]
        # An event with no Vault records nothing. One whose Vault is gone records
        # an inert mark: Vault ids are never reused.
        vaultless = await _insert_event(setup_pool, None, "system.note", "alice")
        gone = uuid.uuid4()
        orphaned = await _insert_event(setup_pool, gone, "document.put", "alice")
        await _publish_long_ago(setup_pool, later + [vaultless, orphaned])

        holder = await asyncpg.connect(_database_dsn(database))
        try:
            hold = holder.transaction()
            await hold.start()
            # Hold the Vault's watermark row: the sweep's delete cannot record its new mark.
            await holder.execute(
                "SELECT 1 FROM event_retention_watermarks WHERE vault_id = $1 FOR UPDATE", vault_id
            )
            sweeping = asyncio.create_task(_sweep(monkeypatch, setup_pool))
            await asyncio.sleep(0.5)
            assert not sweeping.done()
            # The delete waits with the record of it: no one sees the events gone yet.
            assert await setup_pool.fetchval(
                "SELECT count(*) FROM events WHERE id = ANY($1::bigint[])", later
            ) == len(later)
            await hold.rollback()
            assert await asyncio.wait_for(sweeping, 5) == 4
        finally:
            await holder.close()

        assert await setup_pool.fetchval("SELECT count(*) FROM events") == 0
        assert await _watermarks(setup_pool) == {vault_id: later[-1], gone: orphaned}
        # The Vault's delete takes its row with it.
        await setup_pool.execute("DELETE FROM vaults WHERE id = $1", vault_id)
        assert await _watermarks(setup_pool) == {gone: orphaned}


async def test_the_watermark_starts_at_the_old_boundary_for_vaults_that_have_events(monkeypatch):
    """Deletions before migration 133 left no record.

    A Vault that still has events starts just below its earliest one, where
    the tail drew the line before, so history that may already have been swept
    is still refused. A Vault with no events gets no row. Events of a Vault
    that no longer exists get an inert one: nothing reads it, as Vault ids
    are never reused.
    """
    async with _fresh_database() as setup_pool:
        with_events = await _make_vault(setup_pool, "retention-backfill")
        await _make_vault(setup_pool, "retention-backfill-empty")
        gone = uuid.uuid4()
        gone_first = await _insert_event(setup_pool, gone, "document.put", "alice")
        for _ in range(4):
            await _insert_event(setup_pool, gone, "document.put", "alice")
        earliest = await _insert_event(setup_pool, with_events, "document.put", "alice")
        await _insert_event(setup_pool, with_events, "document.update", "alice")
        await _as_before_the_migration(setup_pool)

        async with setup_pool.acquire() as conn:
            await _install_and_backfill(conn)
        assert await _watermarks(setup_pool) == {with_events: earliest - 1, gone: gone_first - 1}
        assert await _trigger_installed(setup_pool)

        # Applying them again keeps a mark raised since.
        await setup_pool.execute(
            "UPDATE event_retention_watermarks SET deleted_through = $2 WHERE vault_id = $1", with_events, earliest
        )
        async with setup_pool.acquire() as conn:
            await _install_and_backfill(conn)
        assert await _watermarks(setup_pool) == {with_events: earliest, gone: gone_first - 1}


async def test_a_deletion_before_the_migration_cannot_be_reconstructed(monkeypatch):
    """The starting mark cannot recover a deletion that left an earlier event in place.

    Of a Vault's two events, the later one alone was deleted before migration
    133, which left no record of it. The mark starts just below the earlier,
    kept event, so a cursor at that event resumes with no 410 although it never
    received the deleted one: the boundary the tail used before had the same
    limit, and nothing in the database tells the two cases apart.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "retention-partial")
        user_id = await _make_reader(setup_pool, "retention-partial-reader", [vault_id])
        await _as_before_the_migration(setup_pool)
        kept = await _insert_event(setup_pool, vault_id, "document.put", "alice")
        deleted = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        await setup_pool.execute("DELETE FROM events WHERE id = $1", deleted)
        async with setup_pool.acquire() as conn:
            await _install_and_backfill(conn)
        assert await _watermarks(setup_pool) == {vault_id: kept - 1}

        later = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        codec = EventCursorCodec(_CURSOR_SECRET)
        async with _tail(monkeypatch, setup_pool) as tail:
            resumed = await tail.open(
                user_id=user_id,
                vault="retention-partial",
                last_event_id=codec.encode(vault_id, (), kept),
            )
            try:
                delivered = await asyncio.wait_for(_changes(resumed, 1), 5)
            finally:
                await resumed.aclose()

        # The deleted event is passed over without a 410.
        assert _positions(delivered) == [later]


async def test_applying_the_migration_again_sets_no_starting_mark():
    """Once the trigger records deletions, a starting mark would only claim one that never happened.

    A Vault that was empty when the migration first ran, and has an event
    since, used to get a mark below that event from a second application, and
    a cursor taken while it was empty was then refused.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "retention-reapplied")
        await _insert_event(setup_pool, vault_id, "document.put", "alice")
        async with setup_pool.acquire() as conn:
            await _install_and_backfill(conn)
            await _install_and_backfill(conn)

        assert await _watermarks(setup_pool) == {}
        assert await _trigger_installed(setup_pool)
        assert await setup_pool.fetchval("SELECT count(*) FROM event_retention_backfill") == 0


async def test_a_delete_by_an_earlier_release_records_the_watermark(monkeypatch):
    """During a rolling deployment, or after a rollback, an earlier release's sweeper deletes events.

    It knows nothing of the watermark, and its deletions used to go unrecorded,
    so a cursor that never received a deleted event resumed past it.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "retention-earlier-release")
        user_id = await _make_reader(setup_pool, "retention-earlier-reader", [vault_id])
        event_ids = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        await _publish_long_ago(setup_pool, event_ids[:2])
        assert await setup_pool.fetchval(_EARLIER_SWEEP) == 2
        # And an operator's delete by hand.
        await setup_pool.execute("DELETE FROM events WHERE id = $1", event_ids[2])

        assert await _watermarks(setup_pool) == {vault_id: event_ids[2]}
        codec = EventCursorCodec(_CURSOR_SECRET)
        async with _tail(monkeypatch, setup_pool) as tail:
            with pytest.raises(EventGapError) as exc_info:
                await tail.open(
                    user_id=user_id,
                    vault="retention-earlier-release",
                    last_event_id=codec.encode(vault_id, (), event_ids[0]),
                )
        assert codec.decode(exc_info.value.details["earliest_cursor"]) == (vault_id, (), event_ids[2])


@pytest.mark.parametrize("first", ["delete", "migration"])
async def test_a_delete_during_the_install_is_recorded_or_waits(first: str):
    """No delete falls between the backfill boundary and the trigger.

    A delete already under way when migration 133 starts finishes first, and
    the starting marks see its result; one that starts while 133 runs waits
    for it, and the trigger records it.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-migrating")
        event_ids = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        migrating = await asyncpg.connect(_database_dsn(database))
        deleting = await asyncpg.connect(_database_dsn(database))
        try:
            if first == "delete":
                deletion = deleting.transaction()
                await deletion.start()
                await deleting.execute("DELETE FROM events WHERE id = $1", event_ids[0])
                applying = asyncio.create_task(migration.migrate(conn=migrating))
                await asyncio.sleep(0.3)
                # The migration waits for the delete under way.
                assert not applying.done()
                await deletion.commit()
                await asyncio.wait_for(applying, 5)
            else:
                migration_tx = migrating.transaction()
                await migration_tx.start()
                await migration.migrate(conn=migrating)
                deletion_task = asyncio.create_task(deleting.execute("DELETE FROM events WHERE id = $1", event_ids[0]))
                await asyncio.sleep(0.3)
                # The delete waits for the migration to commit.
                assert not deletion_task.done()
                await migration_tx.commit()
                await asyncio.wait_for(deletion_task, 5)
            await backfill.migrate(conn=migrating)
        finally:
            await migrating.close()
            await deleting.close()

        # Either way the deleted event is below the mark.
        assert await _watermarks(setup_pool) == {vault_id: event_ids[0]}


async def test_later_event_writes_go_through_while_the_migration_waits_for_its_lock():
    """A lock request that waits queues every later event insert, of any Vault, behind it.

    While a long transaction holds an event it wrote, the migration's request
    for the events lock waits. It used to wait the migration runner's
    5-second lock timeout on each attempt, and every later insert waited
    behind it all that time. Each attempt now gives up after 500 ms and holds
    nothing while it backs off; the migration finishes once the long
    transaction ends.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        busy = await _make_vault(setup_pool, "retention-lock-busy")
        other = await _make_vault(setup_pool, "retention-lock-other")
        await _as_before_the_migration(setup_pool)
        await setup_pool.execute("DELETE FROM schema_migrations WHERE filename = $1", _MIGRATION)
        migration = _load_migration(_MIGRATION)
        assert migration is not None
        long_writer = await asyncpg.connect(_database_dsn(database))
        later_writer = await asyncpg.connect(_database_dsn(database))
        migrator = await asyncpg.connect(_database_dsn(database))
        applying: asyncio.Task[None] | None = None
        worst = 0.0
        try:
            holding = long_writer.transaction()
            await holding.start()
            await _write_event(long_writer, busy)
            applying = asyncio.create_task(_run_one_migration(migrator, _MIGRATION, migration))
            await _waits_for(setup_pool, migrator, long_writer)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                started = time.monotonic()
                await _write_event(later_writer, other)
                worst = max(worst, time.monotonic() - started)
                await asyncio.sleep(0.05)
            # The migration still waits for the long transaction.
            assert not applying.done()
            await holding.commit()
            await asyncio.wait_for(applying, 10)
        finally:
            if applying is not None and not applying.done():
                applying.cancel()
                with contextlib.suppress(BaseException):
                    await applying
            for conn in (long_writer, later_writer, migrator):
                await conn.close()

        assert worst < 1.0, f"a later event insert waited {worst:.2f}s behind the migration"
        assert await _trigger_installed(setup_pool)
        assert await setup_pool.fetchval("SELECT count(*) FROM schema_migrations WHERE filename = $1", _MIGRATION) == 1


async def test_the_migration_gives_up_with_an_error_that_names_the_cause(monkeypatch):
    """A transaction that keeps its event open past every attempt stops the migration, not the writers."""
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-lock-bound")
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        assert migration is not None
        monkeypatch.setattr(migration, "ATTEMPTS", 3)
        monkeypatch.setattr(migration, "FIRST_BACKOFF_SECONDS", 0.05)
        long_writer = await asyncpg.connect(_database_dsn(database))
        migrator = await asyncpg.connect(_database_dsn(database))
        try:
            holding = long_writer.transaction()
            await holding.start()
            await _write_event(long_writer, vault_id)
            with pytest.raises(RuntimeError, match="could not lock the events table in 3 attempts"):
                await asyncio.wait_for(migration.migrate(conn=migrator), 10)
            await holding.rollback()
        finally:
            await long_writer.close()
            await migrator.close()

        # Nothing of the migration stayed behind.
        assert not await _trigger_installed(setup_pool)
        assert await _watermarks(setup_pool) == {}
        assert await setup_pool.fetchval("SELECT count(*) FROM event_retention_backfill") == 0


async def test_applying_the_install_again_asks_for_no_lock(monkeypatch):
    """With the trigger in place, migration 133 has nothing to do, and waits for nothing.

    It used to ask for the events lock before it looked for the trigger, so
    applying it again beside a transaction that kept an event open waited
    through every attempt and then failed.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-reinstall")
        migration = _load_migration(_MIGRATION)
        assert migration is not None
        monkeypatch.setattr(migration, "ATTEMPTS", 3)
        monkeypatch.setattr(migration, "FIRST_BACKOFF_SECONDS", 0.05)
        long_writer = await asyncpg.connect(_database_dsn(database))
        migrator = await asyncpg.connect(_database_dsn(database))
        try:
            holding = long_writer.transaction()
            await holding.start()
            await _write_event(long_writer, vault_id)
            started = time.monotonic()
            await asyncio.wait_for(migration.migrate(conn=migrator), 10)
            elapsed = time.monotonic() - started
            await holding.rollback()
        finally:
            await long_writer.close()
            await migrator.close()

        assert elapsed < 0.5
        assert await _trigger_installed(setup_pool)
        assert await setup_pool.fetchval("SELECT count(*) FROM event_retention_backfill") == 0


async def test_an_install_that_finds_the_trigger_under_its_lock_adds_nothing():
    """Two installs at once: the second saw no trigger before it asked for the lock, and finds it once it holds it."""
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        assert migration is not None
        first = await asyncpg.connect(_database_dsn(database))
        second = await asyncpg.connect(_database_dsn(database))
        try:
            installing = first.transaction()
            await installing.start()
            await migration.migrate(conn=first)
            again = asyncio.create_task(migration.migrate(conn=second))
            await _waits_for(setup_pool, second, first)
            await installing.commit()
            await asyncio.wait_for(again, 10)
        finally:
            await first.close()
            await second.close()

        assert (
            await setup_pool.fetchval(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'events'::regclass "
                "AND tgname = 'akb_events_record_deletions'"
            )
            == 1
        )
        # One boundary, the first install's.
        assert await setup_pool.fetchval("SELECT count(*) FROM event_retention_backfill") == 1


async def test_the_install_reads_no_events_under_its_lock_even_when_deleted_rows_await_cleanup():
    """Migration 133 holds inserts and deletes on events, so it must not read them.

    It first set the starting marks under that lock with a grouped scan of
    every event; then it read the highest id from the end of the primary key,
    which is quick only when that end holds a live row. After a large delete
    whose rows no cleanup could remove yet, because an older snapshot still
    saw them, that read walked every dead entry under the lock. The boundary
    now comes from the id sequence, and a Vault whose first event comes after
    the install still gets no mark.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        swept = await _make_vault(setup_pool, "retention-swept-all")
        later = await _make_vault(setup_pool, "retention-after-sweep")
        await _as_before_the_migration(setup_pool)
        await setup_pool.execute(
            """
            INSERT INTO events (vault_id, kind, resource_uri, actor_id, payload)
            SELECT $1, 'document.put', 'akb://event-test/document.put', 'alice', '{}'::jsonb
              FROM generate_series(1, 20000)
            """,
            swept,
        )
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        older = await asyncpg.connect(_database_dsn(database))
        # A connection of its own, so its statistics count the install alone.
        installing = await asyncpg.connect(_database_dsn(database))
        try:
            # An older snapshot still sees the rows deleted next, so no
            # cleanup can remove them or mark their index entries dead.
            seeing = older.transaction(isolation="repeatable_read")
            await seeing.start()
            assert await older.fetchval("SELECT count(*) FROM events") == 20000
            await setup_pool.execute("DELETE FROM events WHERE vault_id = $1", swept)
            async with installing.transaction():
                started = time.monotonic()
                await migration.migrate(conn=installing)
                elapsed = time.monotonic() - started
                reads = await installing.fetchrow(
                    """
                    SELECT seq_scan, idx_scan,
                           pg_stat_get_xact_tuples_returned('events_pkey'::regclass) AS entries_read
                      FROM pg_stat_xact_user_tables
                     WHERE relname = 'events'
                    """
                )
            await seeing.rollback()
        finally:
            await older.close()
            await installing.close()

        assert (reads["seq_scan"], reads["idx_scan"]) == (0, 0), dict(reads)
        assert elapsed < 1.0
        assert await _trigger_installed(setup_pool)

        # The first event of a Vault after the install is above the boundary.
        await _insert_event(setup_pool, later, "document.put", "alice")
        async with setup_pool.acquire() as conn:
            await backfill.migrate(conn=conn)
        assert await _watermarks(setup_pool) == {}


async def test_event_writes_go_through_while_the_backfill_runs():
    """Migration 134 reads events without the lock that stops writes."""
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-backfill-writes")
        other = await _make_vault(setup_pool, "retention-backfill-other")
        first = await _insert_event(setup_pool, vault_id, "document.put", "alice")
        await _insert_event(setup_pool, vault_id, "document.update", "alice")
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        async with setup_pool.acquire() as conn:
            async with conn.transaction():
                await migration.migrate(conn=conn)
        backfilling = await asyncpg.connect(_database_dsn(database))
        writer = await asyncpg.connect(_database_dsn(database))
        try:
            setting = backfilling.transaction()
            await setting.start()
            await backfill.migrate(conn=backfilling)
            # The backfill has not committed; a write to events does not wait for it.
            started = time.monotonic()
            await asyncio.wait_for(_write_event(writer, other), 5)
            waited = time.monotonic() - started
            await setting.commit()
        finally:
            await backfilling.close()
            await writer.close()

        assert waited < 0.5
        assert await _watermarks(setup_pool) == {vault_id: first - 1}


@pytest.mark.parametrize("first", ["delete", "backfill"])
async def test_a_delete_racing_the_backfill_is_recorded_and_no_lower_mark_replaces_it(first: str):
    """Migration 134 runs beside deletes, and its marks must not undo the trigger's.

    Whichever writes the Vault's mark first, the other waits for it. If the
    trigger was first, the backfill leaves its mark alone; if the backfill
    was, the trigger raises the starting mark to the deleted id. Either way
    the delete stays recorded, and the starting mark, just below the
    earliest event the backfill saw, does not lower it.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-racing")
        event_ids = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        async with setup_pool.acquire() as conn:
            async with conn.transaction():
                await migration.migrate(conn=conn)
        backfilling = await asyncpg.connect(_database_dsn(database))
        deleting = await asyncpg.connect(_database_dsn(database))
        try:
            setting = backfilling.transaction()
            deletion = deleting.transaction()
            if first == "delete":
                await deletion.start()
                # The trigger writes the Vault's mark; the delete has not committed.
                await deleting.execute("DELETE FROM events WHERE id = $1", event_ids[1])
                await setting.start()
                # The backfill still sees the deleted event, and waits for the mark's row.
                applying = asyncio.create_task(backfill.migrate(conn=backfilling))
                await _waits_for(setup_pool, backfilling, deleting)
                await deletion.commit()
                await asyncio.wait_for(applying, 5)
                await setting.commit()
            else:
                await setting.start()
                await backfill.migrate(conn=backfilling)
                await deletion.start()
                # The trigger waits for the backfill's mark.
                removing = asyncio.create_task(deleting.execute("DELETE FROM events WHERE id = $1", event_ids[1]))
                await _waits_for(setup_pool, deleting, backfilling)
                await setting.commit()
                await asyncio.wait_for(removing, 5)
                await deletion.commit()
        finally:
            await backfilling.close()
            await deleting.close()

        assert await _watermarks(setup_pool) == {vault_id: event_ids[1]}


async def test_the_backfill_gives_no_mark_to_a_vault_whose_events_all_came_after_the_install(monkeypatch):
    """Events above the boundary are the trigger's alone.

    A Vault that was empty when the trigger was installed, and has events by
    the time the backfill runs, would otherwise get a mark just below its
    first event, and a cursor taken while it was empty would be refused.
    """
    async with _fresh_database() as setup_pool:
        older = await _make_vault(setup_pool, "retention-before-install")
        newer = await _make_vault(setup_pool, "retention-after-install")
        user_id = await _make_reader(setup_pool, "retention-after-reader", [newer])
        first_older = await _insert_event(setup_pool, older, "document.put", "alice")
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        # A cursor taken while the newer Vault was empty.
        cursor = EventCursorCodec(_CURSOR_SECRET).encode(newer, (), 0)
        async with setup_pool.acquire() as conn:
            async with conn.transaction():
                await migration.migrate(conn=conn)
        # Between the two migrations: other events, then the newer Vault's first.
        for _ in range(5):
            await _insert_event(setup_pool, older, "document.update", "alice")
        first_newer = await _insert_event(setup_pool, newer, "document.put", "alice")
        async with setup_pool.acquire() as conn:
            await backfill.migrate(conn=conn)

        assert await _watermarks(setup_pool) == {older: first_older - 1}
        async with _tail(monkeypatch, setup_pool) as tail:
            resumed = await tail.open(user_id=user_id, vault="retention-after-install", last_event_id=cursor)
            try:
                delivered = await asyncio.wait_for(_changes(resumed, 1), 5)
            finally:
                await resumed.aclose()
        assert _positions(delivered) == [first_newer]


async def test_the_backfill_keeps_a_mark_the_trigger_recorded_since_the_install(monkeypatch):
    """A Vault's ids interleave with other Vaults', so its next event can be far above a deleted one.

    Its first event was deleted between the two migrations, and the trigger
    recorded exactly that. The backfill used to raise the mark to just below
    the earliest event left, over ids that other Vaults' events use, and a
    cursor at the deleted event, which missed nothing, was refused.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "retention-interleaved")
        user_id = await _make_reader(setup_pool, "retention-interleaved-reader", [vault_id])
        # Events of a Vault that no longer exists take the ids in between, and
        # get no mark of their own.
        gone = uuid.uuid4()
        first = await _insert_event(setup_pool, vault_id, "document.put", "alice")
        for _ in range(5):
            await _insert_event(setup_pool, gone, "document.put", "alice")
        second = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        async with setup_pool.acquire() as conn:
            async with conn.transaction():
                await migration.migrate(conn=conn)
        # Between the two migrations the first event is deleted, and the trigger records it.
        await setup_pool.execute("DELETE FROM events WHERE id = $1", first)
        assert await _watermarks(setup_pool) == {vault_id: first}
        async with setup_pool.acquire() as conn:
            await backfill.migrate(conn=conn)

        assert (await _watermarks(setup_pool))[vault_id] == first
        codec = EventCursorCodec(_CURSOR_SECRET)
        async with _tail(monkeypatch, setup_pool) as tail:
            resumed = await tail.open(
                user_id=user_id,
                vault="retention-interleaved",
                last_event_id=codec.encode(vault_id, (), first),
            )
            try:
                delivered = await asyncio.wait_for(_changes(resumed, 1), 5)
            finally:
                await resumed.aclose()
        assert second > first + 1
        assert _positions(delivered) == [second]


async def test_the_backfill_takes_no_vault_lock():
    """Migration 134 neither reads nor locks a Vault, so no transaction on one holds it up.

    It locked the Vaults it set marks for, so a delete of one of them that was
    still open held up the backfill, and with it the server's start. A mark
    it sets for a Vault deleted meanwhile is inert.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        target = await _make_vault(setup_pool, "retention-locked-target")
        first = await _insert_event(setup_pool, target, "document.put", "alice")
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        async with setup_pool.acquire() as conn:
            async with conn.transaction():
                await migration.migrate(conn=conn)
        holding = await asyncpg.connect(_database_dsn(database))
        try:
            # A delete of the very Vault the backfill sets a mark for, held open.
            deletion = holding.transaction()
            await deletion.start()
            await holding.execute("DELETE FROM vaults WHERE id = $1", target)
            async with setup_pool.acquire() as conn:
                await asyncio.wait_for(backfill.migrate(conn=conn), 2)
            await deletion.rollback()
        finally:
            await holding.close()

        assert await _watermarks(setup_pool) == {target: first - 1}
        assert await setup_pool.fetchval("SELECT count(*) FROM event_retention_backfill") == 0


async def test_ids_a_session_drew_ahead_before_the_install_land_above_the_boundary(monkeypatch):
    """With a sequence cache above 1, a session holds ids it drew ahead and has not used.

    Ids it handed out after the install were at or below the boundary, so the
    backfill counted them as events from before it. A Vault whose first event
    had been deleted before the install got a mark below its next event, and
    a cursor at the deleted event, which missed nothing, was refused. The
    install now sets the cache to 1, which makes the session drop the rest of
    its block, and its next id lands above the boundary.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-cached-ids")
        user_id = await _make_reader(setup_pool, "retention-cached-reader", [vault_id])
        await _as_before_the_migration(setup_pool)
        await setup_pool.execute("ALTER SEQUENCE events_id_seq CACHE 5")
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        holder = await asyncpg.connect(_database_dsn(database))
        try:
            # The session draws a block of five ids with its first event.
            first = await _write_event(holder, vault_id)
            await _write_event(holder, uuid.uuid4())
            # The Vault's first event is deleted before the install records anything.
            await setup_pool.execute("DELETE FROM events WHERE id = $1", first)
            async with setup_pool.acquire() as conn:
                async with conn.transaction():
                    await migration.migrate(conn=conn)
            later = await _write_event(holder, vault_id)
        finally:
            await holder.close()
        async with setup_pool.acquire() as conn:
            await backfill.migrate(conn=conn)

        codec = EventCursorCodec(_CURSOR_SECRET)
        async with _tail(monkeypatch, setup_pool) as tail:
            resumed = await tail.open(
                user_id=user_id,
                vault="retention-cached-ids",
                last_event_id=codec.encode(vault_id, (), first),
            )
            try:
                delivered = await asyncio.wait_for(_changes(resumed, 1), 5)
            finally:
                await resumed.aclose()
        assert _positions(delivered) == [later]
        assert vault_id not in await _watermarks(setup_pool)
        cache = await setup_pool.fetchval("SELECT seqcache FROM pg_sequence WHERE seqrelid = 'events_id_seq'::regclass")
        assert cache == 1


async def test_the_backfill_waits_for_no_vault_it_sets_no_mark_for():
    """Migration 134 locks only the Vaults it sets marks for.

    It used to lock every Vault, so a long transaction on another Vault's
    row, such as a delete, held up the backfill, and with it the server's
    start.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        target = await _make_vault(setup_pool, "retention-backfill-target")
        bystander = await _make_vault(setup_pool, "retention-backfill-bystander")
        first = await _insert_event(setup_pool, target, "document.put", "alice")
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        backfill = _load_migration(_BACKFILL)
        assert migration is not None and backfill is not None
        async with setup_pool.acquire() as conn:
            async with conn.transaction():
                await migration.migrate(conn=conn)
        holding = await asyncpg.connect(_database_dsn(database))
        try:
            # A delete of a Vault with no events holds its row.
            deletion = holding.transaction()
            await deletion.start()
            await holding.execute("DELETE FROM vaults WHERE id = $1", bystander)
            async with setup_pool.acquire() as conn:
                await asyncio.wait_for(backfill.migrate(conn=conn), 2)
            await deletion.rollback()
        finally:
            await holding.close()

        assert await _watermarks(setup_pool) == {target: first - 1}


async def _done_or_waiting(
    pool: asyncpg.Pool, task: asyncio.Future, waiter: asyncpg.Connection, holder: asyncpg.Connection
) -> None:
    """Return once ``task`` is done or ``waiter`` waits for a lock that ``holder`` holds."""

    async def poll() -> None:
        while not task.done() and not await pool.fetchval(
            "SELECT $2 = ANY(pg_blocking_pids($1))", waiter.get_server_pid(), holder.get_server_pid()
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


async def test_a_vault_cleanup_and_a_delete_of_its_events_do_not_deadlock():
    """A Vault cleanup locks its Vault and then deletes the Vault's events.

    The failed create's cleanup does so. A plain delete of the same events
    beside it, an operator's or an earlier release's, held the deleted rows
    while its trigger waited for the Vault row, and the cleanup then waited
    for those rows: PostgreSQL broke the deadlock by failing one of the two.
    Recording a deletion now waits for no Vault row.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-cleanup")
        for _ in range(3):
            await _insert_event(setup_pool, vault_id, "document.put", "alice")
        cleanup = await asyncpg.connect(_database_dsn(database))
        operator = await asyncpg.connect(_database_dsn(database))
        tasks: list[asyncio.Future] = []
        try:
            cleaning = cleanup.transaction()
            await cleaning.start()
            await cleanup.fetchval("SELECT 1 FROM vaults WHERE id = $1 FOR UPDATE", vault_id)
            deleting = operator.transaction()
            await deleting.start()
            operator_delete = asyncio.ensure_future(
                operator.execute("DELETE FROM events WHERE vault_id = $1", vault_id)
            )
            tasks.append(operator_delete)
            await _done_or_waiting(setup_pool, operator_delete, operator, cleanup)
            cleanup_delete = asyncio.ensure_future(cleanup.execute("DELETE FROM events WHERE vault_id = $1", vault_id))
            tasks.append(cleanup_delete)
            # The cleanup waits for the rows the operator's delete holds.
            await _waits_for(setup_pool, cleanup, operator)
            if operator_delete.done() and operator_delete.exception() is None:
                await deleting.commit()
            done, _ = await asyncio.wait(tasks, timeout=5, return_when=asyncio.FIRST_EXCEPTION)
            failures = [task.exception() for task in done if task.exception() is not None]
            assert not failures, failures
            assert operator_delete.done() and cleanup_delete.done()
            await cleanup.execute("DELETE FROM vaults WHERE id = $1", vault_id)
            await cleaning.commit()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await cleanup.close()
            await operator.close()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task

        assert await setup_pool.fetchval("SELECT count(*) FROM events") == 0
        # The operator's delete recorded a mark, and the Vault's delete took it.
        assert await _watermarks(setup_pool) == {}


async def test_a_vault_delete_beside_a_sweep_takes_the_mark_the_sweep_raises(monkeypatch):
    """The sweep and a Vault delete both succeed, and the Vault's mark goes with it.

    The sweep raises the Vault's mark without waiting for the Vault; the
    Vault's delete waits for that mark's row and removes it once the sweep
    commits. A mark a sweep inserts for the first time beside the delete, or
    after it, is left behind, and is inert.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        doomed = await _make_vault(setup_pool, "retention-sweep-doomed")
        kept = await _make_vault(setup_pool, "retention-sweep-kept")
        doomed_ids = [await _insert_event(setup_pool, doomed, "document.put", "alice") for _ in range(2)]
        kept_ids = [await _insert_event(setup_pool, kept, "document.put", "alice") for _ in range(2)]
        # The Vault has a mark already: its first event was deleted earlier.
        await setup_pool.execute("DELETE FROM events WHERE id = $1", doomed_ids[0])
        assert await _watermarks(setup_pool) == {doomed: doomed_ids[0]}
        await _publish_long_ago(setup_pool, doomed_ids[1:] + kept_ids)
        sweeping = await asyncpg.connect(_database_dsn(database))
        deleting = await asyncpg.connect(_database_dsn(database))
        try:
            # The sweeper's delete, still open: the marks are written, not committed.
            sweep = sweeping.transaction()
            await sweep.start()
            assert await sweeping.fetchval(_EARLIER_SWEEP) == 3
            deletion = deleting.transaction()
            await deletion.start()
            removing = asyncio.ensure_future(deleting.execute("DELETE FROM vaults WHERE id = $1", doomed))
            # The Vault's delete waits for the mark the sweep wrote.
            await _waits_for(setup_pool, deleting, sweeping)
            await sweep.commit()
            await asyncio.wait_for(removing, 5)
            await deletion.commit()
        finally:
            await sweeping.close()
            await deleting.close()

        assert await setup_pool.fetchval("SELECT count(*) FROM events") == 0
        assert await _watermarks(setup_pool) == {kept: kept_ids[-1]}


async def test_a_sweep_waits_for_no_vault_delete(monkeypatch):
    """The sweep takes no Vault lock, so a Vault delete still open holds it up no longer.

    Its mark for the Vault being deleted is left behind if the delete commits
    first, and is inert, as Vault ids are never reused.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        doomed = await _make_vault(setup_pool, "retention-sweep-first-doomed")
        kept = await _make_vault(setup_pool, "retention-sweep-first-kept")
        doomed_ids = [await _insert_event(setup_pool, doomed, "document.put", "alice") for _ in range(2)]
        kept_ids = [await _insert_event(setup_pool, kept, "document.put", "alice") for _ in range(2)]
        await _publish_long_ago(setup_pool, doomed_ids + kept_ids)
        deleting = await asyncpg.connect(_database_dsn(database))
        try:
            deletion = deleting.transaction()
            await deletion.start()
            await deleting.execute("DELETE FROM vaults WHERE id = $1", doomed)
            assert await asyncio.wait_for(_sweep(monkeypatch, setup_pool), 2) == 4
            await deletion.commit()
        finally:
            await deleting.close()

        assert await setup_pool.fetchval("SELECT count(*) FROM events") == 0
        assert (await _watermarks(setup_pool))[kept] == kept_ids[-1]


async def test_a_vault_delete_and_a_sweep_take_marks_in_the_same_order():
    """Writers of marks must take their rows in one order, or two of them can deadlock.

    The sweep's trigger raises marks in Vault id order, A before B. The Vault
    delete trigger deleted them in whatever order its plan met them; here
    that is B first, because B's Vault and mark are stored before A's and
    the Vaults are deleted by a filter no index serves, so both the deleted
    Vaults and the marks come in stored order. A session holding A's mark
    FOR KEY SHARE stops the Vault delete between its two marks while it
    holds B's, yet lets the sweep's update of A through. The sweep then
    waited for B, and once the session ended the delete waited for A:
    PostgreSQL failed one of them with a deadlock. The delete trigger now
    locks the marks in Vault id order before it deletes them.
    """
    vault_a = uuid.UUID("00000000-0000-4000-8000-00000000000a")
    vault_b = uuid.UUID("00000000-0000-4000-8000-00000000000b")
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        # B's Vault, events and mark are stored before A's.
        for vault_id, name in ((vault_b, "retention-order-b"), (vault_a, "retention-order-a")):
            await setup_pool.execute(
                "INSERT INTO vaults (id, name, git_path) VALUES ($1, $2, $3)", vault_id, name, f"/tmp/{name}.git"
            )
        b_events = [await _insert_event(setup_pool, vault_b, "document.put", "alice") for _ in range(2)]
        a_events = [await _insert_event(setup_pool, vault_a, "document.put", "alice") for _ in range(2)]
        await setup_pool.execute("DELETE FROM events WHERE id = $1", b_events[0])
        await setup_pool.execute("DELETE FROM events WHERE id = $1", a_events[0])
        assert await _watermarks(setup_pool) == {vault_b: b_events[0], vault_a: a_events[0]}
        await _publish_long_ago(setup_pool, [a_events[1], b_events[1]])
        holder = await asyncpg.connect(_database_dsn(database))
        deleting = await asyncpg.connect(_database_dsn(database))
        sweeping = await asyncpg.connect(_database_dsn(database))
        tasks: list[asyncio.Future] = []
        try:
            holding = holder.transaction()
            await holding.start()
            await holder.fetchval("SELECT 1 FROM event_retention_watermarks WHERE vault_id = $1 FOR KEY SHARE", vault_a)
            deletion = deleting.transaction()
            await deletion.start()
            removing = asyncio.ensure_future(
                deleting.execute("DELETE FROM vaults WHERE git_path LIKE '/tmp/retention-order-%'")
            )
            tasks.append(removing)
            # The Vault delete waits at A's mark.
            await _waits_for(setup_pool, deleting, holder)
            sweep = sweeping.transaction()
            await sweep.start()
            raising = asyncio.ensure_future(sweeping.fetchval(_EARLIER_SWEEP))
            tasks.append(raising)
            await _done_or_waiting(setup_pool, raising, sweeping, deleting)
            await holding.commit()
            if raising.done() and raising.exception() is None:
                await sweep.commit()
            done, _ = await asyncio.wait(tasks, timeout=5, return_when=asyncio.FIRST_EXCEPTION)
            failures = [task.exception() for task in done if task.exception() is not None]
            assert not failures, failures
            assert removing.done() and raising.done()
            assert raising.result() == 2
            await deletion.commit()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for conn in (holder, deleting, sweeping):
                await conn.close()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task

        assert await _watermarks(setup_pool) == {}
