"""PostgreSQL proof that an event tail gap means deleted events, not a low cursor.

The tail refused a cursor more than one below the Vault's earliest retained
event. Event ids are shared by every Vault, so a cursor taken on an empty Vault,
before its first event, is usually far below that event without having missed
anything, and the tail refused it all the same. Retention now records, per
Vault, the highest event id it deleted, in the statement that deletes, and the
tail refuses only a cursor below that.
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


async def test_the_sweeper_records_the_watermark_in_the_statement_that_deletes(monkeypatch):
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
            # Hold the Vault's watermark row: the sweep cannot record its new mark.
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
        # As before the migration: nothing recorded.
        await setup_pool.execute("DELETE FROM event_retention_watermarks")

        migration = _load_migration(_MIGRATION)
        assert migration is not None
        async with setup_pool.acquire() as conn:
            await migration.migrate(conn=conn)
        assert await _watermarks(setup_pool) == {with_events: earliest - 1}

        # Applying it again keeps a mark the sweeper has since raised.
        await setup_pool.execute(
            "UPDATE event_retention_watermarks SET deleted_through = $2 WHERE vault_id = $1", with_events, earliest
        )
        async with setup_pool.acquire() as conn:
            await migration.migrate(conn=conn)
        assert await _watermarks(setup_pool) == {with_events: earliest}
