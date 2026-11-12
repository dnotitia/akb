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
import uuid

import asyncpg
import pytest

from app.db.postgres import _load_migration
from app.services.event_tail_service import EventCursorCodec, EventGapError
from tests.test_event_tail_budget_postgres import _CURSOR_SECRET, _changes, _make_reader, _positions, _tail
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _make_vault, _sweep


pytestmark = pytest.mark.asyncio

_MIGRATION = "133_event_retention_watermark.py"
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
    """Remove what migration 133 installed, keeping its table."""
    await pool.execute("DROP TRIGGER IF EXISTS akb_events_record_deletions ON events")
    await pool.execute("DELETE FROM event_retention_watermarks")


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
        # An event with no Vault and one whose Vault is gone are swept, and record nothing.
        vaultless = await _insert_event(setup_pool, None, "system.note", "alice")
        orphaned = await _insert_event(setup_pool, uuid.uuid4(), "document.put", "alice")
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
        assert await _watermarks(setup_pool) == {vault_id: later[-1]}
        # The row goes with its Vault.
        await setup_pool.execute("DELETE FROM vaults WHERE id = $1", vault_id)
        assert await _watermarks(setup_pool) == {}


async def test_the_watermark_starts_at_the_old_boundary_for_vaults_that_have_events(monkeypatch):
    """Deletions before migration 133 left no record.

    A Vault that still has events starts just below its earliest one, where
    the tail drew the line before, so history that may already have been swept
    is still refused. A Vault with no events, and events of a Vault that no
    longer exists, get no row.
    """
    async with _fresh_database() as setup_pool:
        with_events = await _make_vault(setup_pool, "retention-backfill")
        await _make_vault(setup_pool, "retention-backfill-empty")
        gone = uuid.uuid4()
        for _ in range(5):
            await _insert_event(setup_pool, gone, "document.put", "alice")
        earliest = await _insert_event(setup_pool, with_events, "document.put", "alice")
        await _insert_event(setup_pool, with_events, "document.update", "alice")
        await _as_before_the_migration(setup_pool)

        migration = _load_migration(_MIGRATION)
        assert migration is not None
        async with setup_pool.acquire() as conn:
            await migration.migrate(conn=conn)
        assert await _watermarks(setup_pool) == {with_events: earliest - 1}
        assert await _trigger_installed(setup_pool)

        # Applying it again keeps a mark raised since.
        await setup_pool.execute(
            "UPDATE event_retention_watermarks SET deleted_through = $2 WHERE vault_id = $1", with_events, earliest
        )
        async with setup_pool.acquire() as conn:
            await migration.migrate(conn=conn)
        assert await _watermarks(setup_pool) == {with_events: earliest}


async def test_applying_the_migration_again_sets_no_starting_mark():
    """Once the trigger records deletions, a starting mark would only claim one that never happened.

    A Vault that was empty when the migration first ran, and has an event
    since, used to get a mark below that event from a second application, and
    a cursor taken while it was empty was then refused.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "retention-reapplied")
        await _insert_event(setup_pool, vault_id, "document.put", "alice")
        migration = _load_migration(_MIGRATION)
        assert migration is not None
        async with setup_pool.acquire() as conn:
            await migration.migrate(conn=conn)
            await migration.migrate(conn=conn)

        assert await _watermarks(setup_pool) == {}
        assert await _trigger_installed(setup_pool)


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
async def test_a_delete_during_the_migration_is_recorded_or_waits(first: str):
    """No delete falls between the starting marks and the trigger.

    A delete already under way when the migration starts finishes first, and
    the starting marks see its result; one that starts while the migration
    runs waits for it, and the trigger records it.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "retention-migrating")
        event_ids = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        await _as_before_the_migration(setup_pool)
        migration = _load_migration(_MIGRATION)
        assert migration is not None
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
        finally:
            await migrating.close()
            await deleting.close()

        # Either way the deleted event is below the mark.
        assert await _watermarks(setup_pool) == {vault_id: event_ids[0]}
