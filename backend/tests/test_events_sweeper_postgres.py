"""PostgreSQL proof that the events outbox is swept with or without Redis.

The sweep ran inside the Redis publisher, which starts only when `redis_url`
is set, so without Redis no event was ever deleted and the table grew without
bound. The sweeper now runs either way: with Redis it deletes the events
published more than the retention period ago, as before; without it, the
events that occurred that long ago, oldest first, a batch per statement. Every
delete goes through the trigger that records the Vault's retention watermark.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import asyncpg
import pytest

from app.config import settings
from app.services.event_tail_service import EventCursorCodec, EventGapError
from tests.test_event_tail_budget_postgres import _CURSOR_SECRET, _changes, _make_reader, _positions, _tail
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _make_vault
from tests.test_event_tail_retention_postgres import _done_or_waiting, _publish_long_ago, _waits_for, _watermarks


pytestmark = pytest.mark.asyncio

_RETENTION_DAYS = 7


async def _age(pool: asyncpg.Pool, event_ids: list[int], days: int = 30) -> None:
    """Make events look as if they occurred ``days`` ago."""
    await pool.execute(
        "UPDATE events SET occurred_at = NOW() - make_interval(days => $2) WHERE id = ANY($1::bigint[])",
        event_ids,
        days,
    )


async def _remaining(pool: asyncpg.Pool) -> list[int]:
    return [row["id"] for row in await pool.fetch("SELECT id FROM events ORDER BY id")]


def _use(monkeypatch, pool: asyncpg.Pool, *, redis_url: str = ""):
    """Point the sweeper at ``pool``, with ``redis_url`` set or not, from a fresh pass."""
    from app.services import events_sweeper

    async def get_pool() -> asyncpg.Pool:
        return pool

    monkeypatch.setattr(events_sweeper, "get_pool", get_pool)
    monkeypatch.setattr(events_sweeper.settings, "redis_url", redis_url)
    monkeypatch.setattr(events_sweeper.settings, "event_retention_days", _RETENTION_DAYS)
    monkeypatch.setattr(events_sweeper, "_position", 0)
    return events_sweeper


async def _sweep_pass(monkeypatch, pool: asyncpg.Pool, *, redis_url: str = "") -> list[int]:
    """Run the sweeper's steps against ``pool`` until one deletes nothing; what each deleted."""
    sweeper = _use(monkeypatch, pool, redis_url=redis_url)
    steps: list[int] = []
    while True:
        steps.append(await sweeper.sweep_once())
        if steps[-1] == 0:
            return steps
        assert len(steps) < 100, steps


def _stub_all_but_the_events_workers(monkeypatch, lifecycle) -> None:
    """Keep every worker `start_workers` composes from starting, except the events ones."""
    monkeypatch.setattr(lifecycle, "selected_document_revision_backend", lambda: "bare_git")
    for module, name in (
        (lifecycle.embed_worker, "start"),
        (lifecycle.delete_worker, "start"),
        (lifecycle.notification_worker, "start"),
        (lifecycle.account_deletion_worker, "start"),
        (lifecycle.sso_account_sync, "start"),
        (lifecycle.app_rollout_worker, "start"),
        (lifecycle.external_git_poller, "start"),
        (lifecycle.metadata_worker, "start"),
        (lifecycle.vault_backfill, "start"),
        (lifecycle.queue_rescuer, "start"),
        (lifecycle.m1_file_transfer_reaper, "start"),
        (lifecycle.asset_gc_worker, "start"),
        (lifecycle.s3_delete_worker, "start"),
        (lifecycle.sparse_encoder, "start_tokenizer_pool"),
        (lifecycle.sparse_encoder, "start_stats_refresher"),
        (lifecycle.sparse_encoder, "stop_tokenizer_pool"),
        (lifecycle.write_lane, "start_commit_pool"),
        (lifecycle.write_lane, "stop_commit_pool"),
    ):
        monkeypatch.setattr(module, name, lambda *args, **kwargs: None)


async def test_without_redis_the_worker_lifecycle_deletes_events_past_retention(monkeypatch, tmp_path):
    """Without `redis_url`, the worker lifecycle started nothing that deleted an event.

    The sweep ran inside the Redis publisher, which does not start without
    Redis, so the events table grew without bound. The lifecycle now starts
    the sweeper either way, and its first pass deletes the events that
    occurred more than the retention period ago, through the trigger that
    records the Vault's watermark.
    """
    from tests.test_lifecycle_workers import _import_lifecycle, _settings

    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-lifecycle")
        expired = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        await _age(setup_pool, expired)
        kept = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        monkeypatch.setattr(settings, "redis_url", "")
        lifecycle = _import_lifecycle(monkeypatch, tmp_path)
        _stub_all_but_the_events_workers(monkeypatch, lifecycle)
        # No redis_url: the publisher stays off.
        configured = _settings(external_git_enabled=False)
        configured.worker_shutdown_timeout_secs = 10.0
        monkeypatch.setattr(lifecycle, "settings", configured)

        lifecycle.start_workers(include_api_local=False)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while await _remaining(setup_pool) != [kept] and loop.time() < deadline:
                await asyncio.sleep(0.05)
        finally:
            await lifecycle.stop_workers(include_api_local=False)

        assert await _remaining(setup_pool) == [kept], "events past retention were not deleted"
        assert await _watermarks(setup_pool) == {vault_id: expired[-1]}


async def test_without_redis_a_sweep_deletes_events_that_occurred_before_the_period(monkeypatch):
    """Without Redis nothing publishes, so the period counts from when an event occurred.

    Each delete raises the Vault's watermark through the trigger, so a cursor
    that never received a deleted event is refused with 410 and resumes at the
    watermark, from which the events still kept are delivered.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-unpublished")
        other_vault_id = await _make_vault(setup_pool, "sweep-unpublished-other")
        user_id = await _make_reader(setup_pool, "sweep-unpublished-reader", [vault_id])
        expired: list[int] = []
        other_expired: list[int] = []
        for _ in range(3):
            expired.append(await _insert_event(setup_pool, vault_id, "document.put", "alice"))
            other_expired.append(await _insert_event(setup_pool, other_vault_id, "document.put", "bob"))
        await _age(setup_pool, expired + other_expired)
        # Just inside the period, and well inside it.
        recent = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        await _age(setup_pool, [recent], days=_RETENTION_DAYS - 1)
        latest = await _insert_event(setup_pool, vault_id, "document.update", "alice")

        assert await _sweep_pass(monkeypatch, setup_pool) == [6, 0]

        assert await _remaining(setup_pool) == [recent, latest]
        assert await _watermarks(setup_pool) == {vault_id: expired[-1], other_vault_id: other_expired[-1]}
        codec = EventCursorCodec(_CURSOR_SECRET)
        async with _tail(monkeypatch, setup_pool) as tail:
            with pytest.raises(EventGapError) as exc_info:
                await tail.open(
                    user_id=user_id,
                    vault="sweep-unpublished",
                    last_event_id=codec.encode(vault_id, (), expired[0]),
                )
            details = exc_info.value.details
            assert codec.decode(details["earliest_cursor"]) == (vault_id, (), expired[-1])
            resumed = await tail.open(
                user_id=user_id, vault="sweep-unpublished", last_event_id=details["earliest_cursor"]
            )
            try:
                delivered = await asyncio.wait_for(_changes(resumed, 2), 5)
            finally:
                await resumed.aclose()
        assert _positions(delivered) == [recent, latest]


async def test_without_redis_the_sweep_stops_at_the_first_event_inside_the_period(monkeypatch):
    """A pass reads events from the oldest id and ends at the first one still inside the period.

    An expired event behind it waits until that one expires too, so an event
    is kept at least the period, sometimes a little longer, and a pass with
    nothing to delete reads no further than the oldest event.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-prefix")
        first = await _insert_event(setup_pool, vault_id, "document.put", "alice")
        inside = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        behind = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        await _age(setup_pool, [first, behind])

        assert await _sweep_pass(monkeypatch, setup_pool) == [1, 0]
        assert await _remaining(setup_pool) == [inside, behind]
        assert await _watermarks(setup_pool) == {vault_id: first}

        await _age(setup_pool, [inside])
        assert await _sweep_pass(monkeypatch, setup_pool) == [2, 0]
        assert await _remaining(setup_pool) == []
        assert await _watermarks(setup_pool) == {vault_id: behind}


async def test_without_redis_a_backlog_is_deleted_a_batch_per_statement(monkeypatch):
    """A large backlog is deleted in short statements, each continuing after the last.

    A single statement deleting every expired event would hold all of them,
    and the trigger's transition table of their rows, in one transaction.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-backlog")
        expired = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(5)]
        await _age(setup_pool, expired)
        kept = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        sweeper = _use(monkeypatch, setup_pool)
        monkeypatch.setattr(sweeper, "BATCH_SIZE", 2)

        steps = await _sweep_pass(monkeypatch, setup_pool)

        assert steps == [2, 2, 1, 0]
        assert sweeper._position == 0
        assert await _remaining(setup_pool) == [kept]
        assert await _watermarks(setup_pool) == {vault_id: expired[-1]}


async def test_with_redis_the_sweep_deletes_only_events_published_before_the_period(monkeypatch):
    """With Redis the period counts from publication, as it did, and the setting sets it.

    An event that was never published is kept however old it is, and so is
    one published inside the period even when it occurred long before.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-published")
        unpublished = await _insert_event(setup_pool, vault_id, "document.put", "alice")
        published_long_ago = await _insert_event(setup_pool, vault_id, "document.put", "alice")
        published_lately = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        await _age(setup_pool, [unpublished, published_long_ago, published_lately])
        await _publish_long_ago(setup_pool, [published_long_ago])
        await setup_pool.execute(
            "UPDATE events SET redis_published_at = NOW() - INTERVAL '2 days' WHERE id = $1", published_lately
        )

        # All three are old; the one published long ago is the one deleted.
        assert await _sweep_pass(monkeypatch, setup_pool, redis_url="redis://sweep-test") == [3, 0]
        assert await _remaining(setup_pool) == [unpublished, published_lately]
        assert await _watermarks(setup_pool) == {vault_id: published_long_ago}

        # A shorter period reaches the event published two days ago.
        sweeper = _use(monkeypatch, setup_pool, redis_url="redis://sweep-test")
        monkeypatch.setattr(sweeper.settings, "event_retention_days", 1)
        assert await sweeper.sweep_once() == 2
        assert await _remaining(setup_pool) == [unpublished]
        assert await _watermarks(setup_pool) == {vault_id: published_lately}


async def _hold_a_sweep(conn: asyncpg.Connection, through: int) -> int:
    """Run the sweep's delete for the ids up to ``through`` in ``conn``'s open transaction."""
    from app.services import events_sweeper

    return await conn.fetchval(events_sweeper._DELETE_EXPIRED, 0, through, _RETENTION_DAYS)


async def _done_or_blocked_by(
    pool: asyncpg.Pool, task: asyncio.Future, holder: asyncpg.Connection, *, besides: asyncpg.Connection
) -> None:
    """Return once ``task`` is done or a session other than ``besides`` waits for a lock ``holder`` holds."""

    async def poll() -> None:
        while not task.done() and not await pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid)) AND pid <> $2",
            holder.get_server_pid(),
            besides.get_server_pid(),
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


async def _blocked_by(pool: asyncpg.Pool, holder: asyncpg.Connection) -> None:
    """Return once some session waits for a lock that ``holder`` holds."""

    async def poll() -> None:
        while not await pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))",
            holder.get_server_pid(),
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


async def test_a_sweep_beside_another_sweep_waits_for_no_event_row(monkeypatch):
    """Two processes sweep at once; neither waits for the events the other holds.

    The other process's statement has deleted a Vault's expired events and not
    committed. The sweep here skips those rows, deletes the rest, and ends its
    pass without waiting; the other's commit then records its own deletions.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        held_vault = await _make_vault(setup_pool, "sweep-held")
        free_vault = await _make_vault(setup_pool, "sweep-free")
        held = [await _insert_event(setup_pool, held_vault, "document.put", "alice") for _ in range(3)]
        free = [await _insert_event(setup_pool, free_vault, "document.put", "alice") for _ in range(3)]
        await _age(setup_pool, held + free)
        kept = await _insert_event(setup_pool, free_vault, "document.update", "alice")
        other = await asyncpg.connect(_database_dsn(database))
        try:
            sweeping = other.transaction()
            await sweeping.start()
            assert await _hold_a_sweep(other, held[-1]) == 3
            steps = await asyncio.wait_for(_sweep_pass(monkeypatch, setup_pool), 2)
            await sweeping.commit()
        finally:
            await other.close()

        # Six old events; the three the other statement holds are left to it.
        assert steps == [6, 0]
        assert await _remaining(setup_pool) == [kept]
        assert await _watermarks(setup_pool) == {held_vault: held[-1], free_vault: free[-1]}


async def test_two_sweeps_of_one_vault_wait_only_for_its_mark_and_record_the_highest(monkeypatch):
    """When both sweeps delete events of one Vault, the second waits for that Vault's mark.

    It holds only its own event rows while it waits, and the first holds
    nothing it waits for, so the wait ends at the first one's commit, with no
    deadlock, and the mark keeps the higher of the two.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "sweep-shared")
        expired = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(4)]
        await _age(setup_pool, expired)
        other = await asyncpg.connect(_database_dsn(database))
        tasks: list[asyncio.Future] = []
        try:
            first = other.transaction()
            await first.start()
            assert await _hold_a_sweep(other, expired[1]) == 2
            sweeping = asyncio.ensure_future(_sweep_pass(monkeypatch, setup_pool))
            tasks.append(sweeping)
            # The second sweep deleted the other two events and waits for the mark.
            await _blocked_by(setup_pool, other)
            assert not sweeping.done()
            await first.commit()
            steps = await asyncio.wait_for(sweeping, 5)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await other.close()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task

        assert steps == [4, 0]
        assert await _remaining(setup_pool) == []
        assert await _watermarks(setup_pool) == {vault_id: expired[-1]}


async def test_a_sweep_and_a_vault_delete_take_marks_in_the_same_order(monkeypatch):
    """The sweep's delete writes marks in Vault id order, as the Vault delete locks them.

    As in the earlier release's sweep (test_event_tail_retention_postgres),
    B's Vault and mark are stored before A's, and a session holding A's mark
    FOR KEY SHARE stops the Vault delete at A, the first mark in Vault id
    order, while it holds none, and lets the sweep's update of A through. The
    sweep then writes B's mark, which the Vault delete has not reached; once
    the session ends, the Vault delete waits for the sweep and takes both.
    """
    vault_a = uuid.UUID("00000000-0000-4000-8000-00000000000a")
    vault_b = uuid.UUID("00000000-0000-4000-8000-00000000000b")
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        for vault_id, name in ((vault_b, "sweep-order-b"), (vault_a, "sweep-order-a")):
            await setup_pool.execute(
                "INSERT INTO vaults (id, name, git_path) VALUES ($1, $2, $3)", vault_id, name, f"/tmp/{name}.git"
            )
        b_events = [await _insert_event(setup_pool, vault_b, "document.put", "alice") for _ in range(2)]
        a_events = [await _insert_event(setup_pool, vault_a, "document.put", "alice") for _ in range(2)]
        await setup_pool.execute("DELETE FROM events WHERE id = $1", b_events[0])
        await setup_pool.execute("DELETE FROM events WHERE id = $1", a_events[0])
        assert await _watermarks(setup_pool) == {vault_b: b_events[0], vault_a: a_events[0]}
        await _age(setup_pool, [b_events[1], a_events[1]])
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
                deleting.execute("DELETE FROM vaults WHERE git_path LIKE '/tmp/sweep-order-%'")
            )
            tasks.append(removing)
            await _waits_for(setup_pool, deleting, holder)
            sweep = sweeping.transaction()
            await sweep.start()
            raising = asyncio.ensure_future(_hold_a_sweep(sweeping, a_events[1]))
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

        assert await _remaining(setup_pool) == []
        assert await _watermarks(setup_pool) == {}


@pytest.mark.parametrize("first", ["sweep", "cleanup"])
async def test_a_sweep_beside_a_vault_cleanup_of_the_same_events_does_not_deadlock(monkeypatch, first: str):
    """A failed create's cleanup locks its Vault, then deletes the Vault's events, then the Vault.

    If the sweep deletes the events first, the cleanup waits for those rows,
    holding only its Vault row, which the sweep never waits for. If the
    cleanup is first, the sweep skips the rows it holds and waits for nothing.
    Either way both finish, and the Vault's delete takes its mark.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "sweep-cleanup")
        bystander = await _make_vault(setup_pool, "sweep-cleanup-bystander")
        doomed = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(2)]
        aside = [await _insert_event(setup_pool, bystander, "document.put", "alice") for _ in range(2)]
        await _age(setup_pool, doomed + aside)
        fresh = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        cleanup = await asyncpg.connect(_database_dsn(database))
        other = await asyncpg.connect(_database_dsn(database))
        tasks: list[asyncio.Future] = []
        try:
            cleaning = cleanup.transaction()
            await cleaning.start()
            await cleanup.fetchval("SELECT 1 FROM vaults WHERE id = $1 FOR UPDATE", vault_id)
            if first == "sweep":
                sweeping = other.transaction()
                await sweeping.start()
                assert await _hold_a_sweep(other, aside[-1]) == 4
                deleting_events = asyncio.ensure_future(
                    cleanup.execute("DELETE FROM events WHERE vault_id = $1", vault_id)
                )
                tasks.append(deleting_events)
                # The cleanup waits for the event rows the sweep holds.
                await _waits_for(setup_pool, cleanup, other)
                await sweeping.commit()
                await asyncio.wait_for(deleting_events, 5)
            else:
                await cleanup.execute("DELETE FROM events WHERE vault_id = $1", vault_id)
                # The sweep skips the cleanup's rows and does not wait for it.
                assert await asyncio.wait_for(_sweep_pass(monkeypatch, setup_pool), 2) == [4, 0]
            await cleanup.execute("DELETE FROM vaults WHERE id = $1", vault_id)
            await cleaning.commit()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await cleanup.close()
            await other.close()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task

        # The sweep took the expired events, the cleanup the rest of the Vault's.
        assert await _remaining(setup_pool) == []
        assert await _watermarks(setup_pool) == {bystander: aside[-1]}
        assert fresh > aside[-1]


async def _publish(pool: asyncpg.Pool, event_ids: list[int], days: int) -> None:
    """Mark events as published ``days`` ago."""
    await pool.execute(
        "UPDATE events SET redis_published_at = NOW() - make_interval(days => $2) WHERE id = ANY($1::bigint[])",
        event_ids,
        days,
    )


async def test_with_redis_a_backlog_is_deleted_a_batch_per_statement_past_unpublished_events(monkeypatch):
    """With Redis the sweep deleted every published event past the period in one unbounded statement.

    It now reads the same batches as without Redis, and each batch's statement
    deletes only the batch's events published that long ago. An old event not
    yet published is kept and does not end the pass.
    """
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-published-backlog")
        events = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(6)]
        await _age(setup_pool, events)
        published = [events[0], events[2], events[3], events[5]]
        unpublished = [events[1], events[4]]
        await _publish(setup_pool, published, days=8)
        young = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        sweeper = _use(monkeypatch, setup_pool, redis_url="redis://sweep-test")
        monkeypatch.setattr(sweeper, "BATCH_SIZE", 2)

        # The first statement reaches only the first batch.
        assert await sweeper.sweep_once() == 2
        assert await _remaining(setup_pool) == events[1:] + [young]
        steps = [2]
        while steps[-1]:
            steps.append(await sweeper.sweep_once())

        assert steps == [2, 2, 2, 0]
        assert await _remaining(setup_pool) == unpublished + [young]
        assert await _watermarks(setup_pool) == {vault_id: events[5]}


async def test_with_redis_a_sweep_beside_an_operator_delete_of_the_same_events_does_not_deadlock(monkeypatch):
    """An operator's delete takes a Vault's events in id order; a plan can take them in publication order.

    A, B and C were published in the order C, B, A, and a session holds B. The
    operator's delete takes A and waits for B. The earlier sweep, one statement
    over the published events, waited for the rows other statements held: read
    from their index it took C and waited for B as well, and once B was free
    the operator took it and waited for C while the sweep waited for B, and
    PostgreSQL failed one of them as a deadlock. The sweep now skips the rows
    another statement holds, so it finishes while they are held, and waits for
    neither.
    """
    async with _fresh_database() as setup_pool:
        database = await setup_pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(setup_pool, "sweep-operator")
        a, b, c = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        await _age(setup_pool, [a, b, c], days=11)
        for event_id, days in ((c, 10), (b, 9), (a, 8)):
            await _publish(setup_pool, [event_id], days=days)
        young = await _insert_event(setup_pool, vault_id, "document.update", "alice")
        # Planner settings under which each statement takes its rows in the
        # order of an index, plans PostgreSQL may choose: the operator's delete
        # a Vault's events by id, a sweep over published events by publication.
        # Without them this small table is read in the order its rows are
        # stored, the same for both.
        by_index = {"enable_seqscan": "off", "enable_bitmapscan": "off"}
        sweep_pool = await asyncpg.create_pool(
            _database_dsn(database), min_size=1, max_size=1, server_settings=by_index
        )
        holder = await asyncpg.connect(_database_dsn(database))
        operator = await asyncpg.connect(_database_dsn(database), server_settings=by_index)
        tasks: list[asyncio.Future] = []
        finished_while_held = False
        try:
            holding = holder.transaction()
            await holding.start()
            await holder.fetchval("SELECT 1 FROM events WHERE id = $1 FOR UPDATE", b)
            deleting = asyncio.ensure_future(
                operator.execute("DELETE FROM events WHERE vault_id = $1 AND id <> $2", vault_id, young)
            )
            tasks.append(deleting)
            # The operator's delete holds A and waits for B.
            await _waits_for(setup_pool, operator, holder)
            sweeping = asyncio.ensure_future(_sweep_pass(monkeypatch, sweep_pool, redis_url="redis://sweep-test"))
            tasks.append(sweeping)
            try:
                await asyncio.wait_for(asyncio.shield(sweeping), 2)
                finished_while_held = True
            except TimeoutError:
                pass
            await holding.commit()
            done, _ = await asyncio.wait(tasks, timeout=5)
            failures = [task.exception() for task in done if task.exception() is not None]
            assert not failures, failures
            assert deleting.done() and sweeping.done()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await holder.close()
            await operator.close()
            await sweep_pool.close()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task

        assert finished_while_held, "the sweep waited for rows another statement held"
        # The sweep took C; the operator took A and B.
        assert sweeping.result() == [3, 0]
        assert await _remaining(setup_pool) == [young]
        assert (await _watermarks(setup_pool))[vault_id] == c


async def test_the_health_counters_count_publication_work_only_with_redis(monkeypatch):
    """With Redis the outbox's unpublished events are work; without it they never will be published."""
    from app.services import events_publisher

    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-health")
        waiting, retried, abandoned, published = [
            await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(4)
        ]
        await setup_pool.execute("UPDATE events SET attempts = 2 WHERE id = $1", retried)
        await setup_pool.execute("UPDATE events SET attempts = 8, abandoned_at = NOW() WHERE id = $1", abandoned)
        await setup_pool.execute("UPDATE events SET redis_published_at = NOW() WHERE id = $1", published)

        async def get_pool() -> asyncpg.Pool:
            return setup_pool

        monkeypatch.setattr(events_publisher, "get_pool", get_pool)
        monkeypatch.setattr(events_publisher.settings, "redis_url", "redis://health-test")
        with_redis = await events_publisher.pending_stats()
        monkeypatch.setattr(events_publisher.settings, "redis_url", "")
        without_redis = await events_publisher.pending_stats()

    assert {key: with_redis[key] for key in ("publishing", "pending", "retrying", "abandoned", "published")} == {
        "publishing": "on",
        "pending": 2,
        "retrying": 1,
        "abandoned": 1,
        "published": 1,
    }
    assert waiting not in (retried, abandoned, published)
    assert {key: without_redis[key] for key in ("publishing", "pending", "retrying", "abandoned", "published")} == {
        "publishing": "off",
        "pending": 0,
        "retrying": 0,
        "abandoned": 0,
        "published": 0,
    }


@pytest.mark.parametrize("redis_url", ["", "redis://sweep-test"], ids=["without-redis", "with-redis"])
async def test_an_open_tail_behind_the_sweep_ends_rather_than_skip_the_deleted_events(monkeypatch, redis_url):
    """An open tail read on by cursor and never looked at the watermark again.

    A tail behind the sweep had sent the first of a Vault's expired events
    when the sweep deleted the rest. Its next read passed over them, sent the
    next event and a cursor past them, and the client never learned of the
    gap: a reconnect from that cursor was not refused. Each read now reads the
    watermark with its rows, and the stream ends instead; the reconnect from
    the last cursor it sent is refused with 410 and resumes at the watermark.
    """
    from app.services import event_tail_service

    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-tail")
        user_id = await _make_reader(setup_pool, "sweep-tail-reader", [vault_id])
        expired = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        await _age(setup_pool, expired)
        if redis_url:
            await _publish_long_ago(setup_pool, expired)
        codec = EventCursorCodec(_CURSOR_SECRET)
        async with _tail(monkeypatch, setup_pool) as tail:
            # One event per read, so the stream is between reads after each one.
            monkeypatch.setattr(event_tail_service, "EVENT_BATCH_SIZE", 1)
            body = await tail.open(user_id=user_id, vault="sweep-tail", start="earliest")
            try:
                first = await asyncio.wait_for(_changes(body, 1), 5)
                assert _positions(first) == [expired[0]]
                # The sweep deletes the expired events while the stream waits to
                # read on, and a new event follows them.
                await _sweep_pass(monkeypatch, setup_pool, redis_url=redis_url)
                latest = await _insert_event(setup_pool, vault_id, "document.update", "alice")
                following = await asyncio.wait_for(anext(body, None), 5)
            finally:
                await body.aclose()
            # The stream ended rather than send the new event past the deleted ones.
            assert following is None
            with pytest.raises(EventGapError) as exc_info:
                await tail.open(
                    user_id=user_id, vault="sweep-tail", last_event_id=codec.encode(vault_id, (), expired[0])
                )
            earliest_cursor = exc_info.value.details["earliest_cursor"]
            assert codec.decode(earliest_cursor) == (vault_id, (), expired[-1])
            resumed = await tail.open(user_id=user_id, vault="sweep-tail", last_event_id=earliest_cursor)
            try:
                delivered = await asyncio.wait_for(_changes(resumed, 1), 5)
            finally:
                await resumed.aclose()
        assert _positions(delivered) == [latest]


async def test_an_open_tail_goes_on_when_only_events_it_sent_are_deleted(monkeypatch):
    """A watermark at or below the stream's position claims nothing it missed."""
    from app.services import event_tail_service

    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "sweep-tail-sent")
        user_id = await _make_reader(setup_pool, "sweep-tail-sent-reader", [vault_id])
        expired = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        await _age(setup_pool, expired)
        async with _tail(monkeypatch, setup_pool) as tail:
            monkeypatch.setattr(event_tail_service, "EVENT_BATCH_SIZE", 1)
            body = await tail.open(user_id=user_id, vault="sweep-tail-sent", start="earliest")
            try:
                sent = await asyncio.wait_for(_changes(body, 3), 5)
                await _sweep_pass(monkeypatch, setup_pool)
                latest = await _insert_event(setup_pool, vault_id, "document.update", "alice")
                following = await asyncio.wait_for(_changes(body, 1), 5)
            finally:
                await body.aclose()
        assert _positions(sent) == expired
        assert await _watermarks(setup_pool) == {vault_id: expired[-1]}
        assert _positions(following) == [latest]
