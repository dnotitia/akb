"""PostgreSQL proof that the sweeper removes the retention marks of Vaults that are gone.

Migration 133's marks have no foreign key to vaults. A Vault's delete removes
its mark, but an ordinary delete leaves the Vault's events behind, and
sweeping them later wrote a mark for the Vault that is gone. That mark is
inert, as Vault ids are never reused, but it stayed for good, one row for
every such Vault. Once a pass has nothing left to delete, the sweeper now
removes the marks of Vaults that no longer exist and have no event left.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import uuid

import asyncpg
import pytest

from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _make_vault
from tests.test_event_tail_retention_postgres import _publish_long_ago, _watermarks
from tests.test_events_sweeper_postgres import _age, _blocked_by, _remaining, _sweep_pass, _use


pytestmark = pytest.mark.asyncio


async def _set_mark(pool: asyncpg.Pool, vault_id: uuid.UUID, deleted_through: int) -> None:
    await pool.execute(
        "INSERT INTO event_retention_watermarks (vault_id, deleted_through) VALUES ($1, $2)",
        vault_id,
        deleted_through,
    )


@pytest.mark.parametrize("redis_url", ["", "redis://marks-test"], ids=["without-redis", "with-redis"])
async def test_the_sweeper_removes_the_mark_of_a_vault_that_is_gone_once_its_events_are(monkeypatch, redis_url):
    """The pass that sweeps a deleted Vault's leftover events removes the mark they write.

    A Vault that still exists keeps its mark, whether or not it has events
    left, and so does the Vault the pass did not empty. Events of a Vault id
    that never existed are treated as a deleted Vault's.
    """
    async with _fresh_database() as setup_pool:
        doomed = await _make_vault(setup_pool, "marks-doomed")
        kept = await _make_vault(setup_pool, "marks-kept")
        emptied = await _make_vault(setup_pool, "marks-emptied")
        ghost = uuid.uuid4()
        doomed_events = [await _insert_event(setup_pool, doomed, "document.put", "alice") for _ in range(2)]
        ghost_event = await _insert_event(setup_pool, ghost, "document.put", "alice")
        kept_events = [await _insert_event(setup_pool, kept, "document.put", "alice") for _ in range(2)]
        emptied_event = await _insert_event(setup_pool, emptied, "document.put", "alice")
        await setup_pool.execute(
            "DELETE FROM events WHERE id = ANY($1::bigint[])", [kept_events[0], emptied_event]
        )
        assert await _watermarks(setup_pool) == {kept: kept_events[0], emptied: emptied_event}
        # An ordinary Vault delete leaves the Vault's events behind.
        await setup_pool.execute("DELETE FROM vaults WHERE id = $1", doomed)
        swept = doomed_events + [ghost_event]
        if redis_url:
            await _publish_long_ago(setup_pool, swept)
        else:
            await _age(setup_pool, swept)

        steps = await _sweep_pass(monkeypatch, setup_pool, redis_url=redis_url)

        # The first step deletes the events and writes their marks; the step
        # that ends the pass removes the two of Vaults that are gone.
        assert steps == [3, 0]
        assert await _remaining(setup_pool) == [kept_events[1]]
        assert await _watermarks(setup_pool) == {kept: kept_events[0], emptied: emptied_event}


async def test_the_mark_of_a_vault_that_is_gone_stays_while_an_event_of_it_is_left(monkeypatch):
    """While an event of the Vault id is left, its mark is what that event's 410 would be judged by."""
    async with _fresh_database() as setup_pool:
        doomed = await _make_vault(setup_pool, "marks-partly-swept")
        first = await _insert_event(setup_pool, doomed, "document.put", "alice")
        second = await _insert_event(setup_pool, doomed, "document.update", "alice")
        await setup_pool.execute("DELETE FROM vaults WHERE id = $1", doomed)
        await _age(setup_pool, [first])

        assert await _sweep_pass(monkeypatch, setup_pool) == [1, 0]
        assert await _remaining(setup_pool) == [second]
        assert await _watermarks(setup_pool) == {doomed: first}

        await _age(setup_pool, [second])
        assert await _sweep_pass(monkeypatch, setup_pool) == [1, 0]
        assert await _watermarks(setup_pool) == {}


async def test_the_cleanup_leaves_a_mark_another_statement_holds_and_waits_for_nothing(monkeypatch):
    """A mark another statement holds, as a sweep's trigger does while it raises it, is skipped.

    The cleanup locks marks with SKIP LOCKED, so it never waits for one, and
    a later pass removes the mark once it is free.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        held, free = uuid.uuid4(), uuid.uuid4()
        await _set_mark(setup_pool, held, 10)
        await _set_mark(setup_pool, free, 20)
        holder = await asyncpg.connect(_database_dsn(database))
        try:
            holding = holder.transaction()
            await holding.start()
            await holder.execute(
                "UPDATE event_retention_watermarks SET deleted_through = deleted_through WHERE vault_id = $1", held
            )
            assert await asyncio.wait_for(_sweep_pass(monkeypatch, setup_pool), 2) == [0]
            assert await _watermarks(setup_pool) == {held: 10}
            await holding.commit()
        finally:
            await holder.close()

        assert await _sweep_pass(monkeypatch, setup_pool) == [0]
        assert await _watermarks(setup_pool) == {}


async def test_the_cleanup_takes_the_marks_in_vault_id_order(monkeypatch):
    """Marks are locked in Vault id order, as every writer of marks takes them, whatever order they are stored in."""
    vault_a = uuid.UUID("00000000-0000-4000-8000-00000000000a")
    vault_b = uuid.UUID("00000000-0000-4000-8000-00000000000b")
    vault_c = uuid.UUID("00000000-0000-4000-8000-00000000000c")
    async with _fresh_database() as setup_pool:
        for vault_id, deleted_through in ((vault_c, 3), (vault_a, 1), (vault_b, 2)):
            await _set_mark(setup_pool, vault_id, deleted_through)
        sweeper = _use(monkeypatch, setup_pool)
        monkeypatch.setattr(sweeper, "MARK_BATCH_SIZE", 2)

        async with setup_pool.acquire() as conn:
            assert await sweeper._delete_orphaned_marks(conn) == 2
            assert await _watermarks(setup_pool) == {vault_c: 3}
            assert await sweeper._delete_orphaned_marks(conn) == 1
            assert await sweeper._delete_orphaned_marks(conn) == 0


async def test_a_vault_delete_beside_the_cleanup_keeps_its_own_mark_and_waits_for_nothing(monkeypatch):
    """A Vault whose delete has not committed still exists for the cleanup, and its mark is held.

    The cleanup leaves that mark to the Vault's delete, which removes it, and
    removes the marks of Vaults already gone, without waiting for the delete.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "marks-being-deleted")
        only = await _insert_event(setup_pool, vault_id, "document.put", "alice")
        await setup_pool.execute("DELETE FROM events WHERE id = $1", only)
        ghost = uuid.uuid4()
        await _set_mark(setup_pool, ghost, 5)
        assert await _watermarks(setup_pool) == {vault_id: only, ghost: 5}
        deleting = await asyncpg.connect(_database_dsn(database))
        try:
            deletion = deleting.transaction()
            await deletion.start()
            await deleting.execute("DELETE FROM vaults WHERE id = $1", vault_id)
            assert await asyncio.wait_for(_sweep_pass(monkeypatch, setup_pool), 2) == [0]
            assert await _watermarks(setup_pool) == {vault_id: only}
            await deletion.commit()
        finally:
            await deleting.close()

        assert await _watermarks(setup_pool) == {}


async def test_a_sweep_waits_for_a_cleanup_that_holds_its_mark_and_writes_the_mark_again(monkeypatch):
    """A cleanup holds the marks it deletes until it commits, and waits for nothing meanwhile.

    An event written for the Vault after the cleanup read the events, and
    swept at once, raises a mark the cleanup holds: the sweep's trigger waits
    for the cleanup to commit and then writes the mark again, and the
    sweeper's next step removes it.
    """
    from app.services import events_sweeper

    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        ghost = uuid.uuid4()
        await _set_mark(setup_pool, ghost, 5)
        cleaner = await asyncpg.connect(_database_dsn(database))
        tasks: list[asyncio.Future] = []
        try:
            cleaning = cleaner.transaction()
            await cleaning.start()
            assert await cleaner.fetchval(events_sweeper._DELETE_ORPHANED_MARKS, 1000) == 1
            late = await _insert_event(setup_pool, ghost, "document.put", "alice")
            await _age(setup_pool, [late])
            sweeping = asyncio.ensure_future(_sweep_pass(monkeypatch, setup_pool))
            tasks.append(sweeping)
            await _blocked_by(setup_pool, cleaner)
            assert not sweeping.done()
            await cleaning.commit()
            steps = await asyncio.wait_for(sweeping, 5)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await cleaner.close()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task

        assert steps == [1, 0]
        assert await _remaining(setup_pool) == []
        assert await _watermarks(setup_pool) == {}


async def test_every_writer_of_events_and_marks_at_once_never_deadlocks(monkeypatch):
    """Sweeps in two processes, a sweep with Redis, the marks' cleanup, Vault deletes, cleanups and an operator at once.

    Every statement that deletes events takes their rows first and the marks
    after, in Vault id order; a Vault delete takes its Vault rows and then its
    marks, in Vault id order; the sweeps, with Redis or without, and the
    marks' cleanup skip rows another statement holds. No wait among them
    closes a cycle, so PostgreSQL never fails one as a deadlock (40P01), and
    none fails at all.
    """
    from app.services import events_sweeper

    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        _use(monkeypatch, setup_pool)
        live = [await _make_vault(setup_pool, f"marks-stress-{index}") for index in range(8)]
        rng = random.Random(4113)

        async def write_events(conn: asyncpg.Connection, vault_ids: list[uuid.UUID], count: int) -> None:
            # Old events, half of them published long ago, so both kinds of sweep have work.
            await conn.execute(
                """
                INSERT INTO events (vault_id, kind, resource_uri, actor_id, payload, occurred_at, redis_published_at)
                SELECT v, 'document.put', 'akb://marks-stress/document.put', 'alice', '{}'::jsonb,
                       NOW() - INTERVAL '30 days',
                       CASE WHEN g % 2 = 0 THEN NOW() - INTERVAL '8 days' END
                  FROM unnest($1::uuid[]) AS v, generate_series(1, $2) AS g
                """,
                vault_ids,
                count,
            )

        async with setup_pool.acquire() as conn:
            await write_events(conn, live, 40)

        deadline = asyncio.get_running_loop().time() + 4.0
        counts = {"swept": 0, "marks_removed": 0, "vaults_deleted": 0, "cleanups": 0}

        def running() -> bool:
            return asyncio.get_running_loop().time() < deadline

        async def sweep(conn: asyncpg.Connection, delete: str) -> None:
            # One process's sweeper: batches of its pass, then the marks.
            position = 0
            while running():
                old, deleted, position = await events_sweeper._sweep_batch(conn, position, delete)
                counts["swept"] += deleted
                if not old:
                    counts["marks_removed"] += await events_sweeper._delete_orphaned_marks(conn)
                    await asyncio.sleep(0.005)

        async def sweep_without_redis(conn: asyncpg.Connection) -> None:
            await sweep(conn, events_sweeper._DELETE_EXPIRED)

        async def sweep_with_redis(conn: asyncpg.Connection) -> None:
            await sweep(conn, events_sweeper._DELETE_PUBLISHED)

        async def delete_vaults(conn: asyncpg.Connection) -> None:
            # As the Vault delete does: lock the Vaults, then delete them. It
            # leaves their events, which the sweeps then delete; their marks
            # are the ones the cleanup removes.
            while running():
                pair = sorted(rng.sample(live, 2))
                async with conn.transaction():
                    await conn.fetch("SELECT id FROM vaults WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE", pair)
                    await conn.execute("DELETE FROM vaults WHERE id = ANY($1::uuid[])", pair)
                counts["vaults_deleted"] += 2
                for vault_id in pair:
                    live.remove(vault_id)
                fresh = [await _make_vault(setup_pool, f"marks-stress-{uuid.uuid4().hex[:12]}") for _ in pair]
                await write_events(conn, fresh, 10)
                live.extend(fresh)
                await asyncio.sleep(0.02)

        async def clean_up_failed_creates(conn: asyncpg.Connection) -> None:
            # As a failed create's cleanup does: lock the new Vault, delete its
            # (new) events, then the Vault.
            while running():
                vault_id = await _make_vault(setup_pool, f"marks-stress-new-{uuid.uuid4().hex[:12]}")
                await conn.execute(
                    "INSERT INTO events (vault_id, kind, payload) SELECT $1::uuid, 'document.put', '{}'::jsonb "
                    "FROM generate_series(1, 3)",
                    vault_id,
                )
                async with conn.transaction():
                    await conn.fetchval("SELECT 1 FROM vaults WHERE id = $1 FOR UPDATE", vault_id)
                    await conn.execute("DELETE FROM events WHERE vault_id = $1", vault_id)
                    await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)
                counts["cleanups"] += 1
                await asyncio.sleep(0.01)

        async def delete_events_by_hand(conn: asyncpg.Connection) -> None:
            # An operator's deletes: one event, or all of a Vault's in one statement.
            while running():
                await conn.execute(
                    "DELETE FROM events WHERE id = (SELECT id FROM events ORDER BY id DESC OFFSET $1 LIMIT 1)",
                    rng.randrange(50),
                )
                if rng.random() < 0.2:
                    await conn.execute("DELETE FROM events WHERE vault_id = $1", rng.choice(live))
                await asyncio.sleep(0.005)

        async def keep_writing(conn: asyncpg.Connection) -> None:
            while running():
                await write_events(conn, rng.sample(live, 3), 5)
                await asyncio.sleep(0.01)

        workers = [
            sweep_without_redis,
            sweep_without_redis,
            sweep_with_redis,
            delete_vaults,
            clean_up_failed_creates,
            delete_events_by_hand,
            keep_writing,
        ]
        connections = [await asyncpg.connect(_database_dsn(database)) for _ in workers]
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    *(worker(conn) for worker, conn in zip(workers, connections)), return_exceptions=True
                ),
                60,
            )
        finally:
            for conn in connections:
                await conn.close()

        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        assert not failures, failures
        assert counts["swept"] and counts["marks_removed"] and counts["vaults_deleted"] and counts["cleanups"], counts
        # Once everything left is swept, no mark of a Vault that is gone remains.
        await _age(setup_pool, await _remaining(setup_pool))
        await _sweep_pass(monkeypatch, setup_pool)
        assert await _remaining(setup_pool) == []
        assert not await setup_pool.fetchval(
            "SELECT count(*) FROM event_retention_watermarks w WHERE NOT EXISTS (SELECT 1 FROM vaults v WHERE v.id = w.vault_id)"
        )
