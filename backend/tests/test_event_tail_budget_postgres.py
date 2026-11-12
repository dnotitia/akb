"""PostgreSQL proof that event tail load leaves the request pool to requests.

A waiting stream holds no connection, but each wake-up used to borrow one for
an access check and another for the batch read, and every row sent checked
access again on a connection of its own. Every event woke every stream, so a
burst multiplied queries by the number of open streams, and when the database
slowed down the woken streams took every connection the pool had: requests,
the readiness probe's among them, then waited past their deadline.

Event tail work now borrows connections only within a per-process budget, one
per pass, and checks access once per batch it read. These tests hold it to
that with the real access check against a real database.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable
import contextlib
import math
from typing import Any
import uuid

import asyncpg
import pytest

from app.config import settings
from app.repositories.events_repo import list_vault_events
from app.services import access_service, event_tail_service
from app.services.event_tail_service import EventCursorCodec, format_heartbeat
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _listening_hub, _make_vault


pytestmark = pytest.mark.asyncio

_POOL_SIZE = 4
_BUDGET = 2
# The readiness probe's whole budget for reaching the database.
_PROBE_BUDGET_SECONDS = 2.0
_PROBE_INTERVAL_SECONDS = 0.1
_CURSOR_SECRET = "cursor-secret"


async def _until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _TailPool:
    """The request pool as event tail code sees it, counting the connections it holds."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.held = 0
        self.most_held = 0

    def acquire(self, *args: Any, **kwargs: Any) -> _TailAcquire:
        return _TailAcquire(self, self._pool.acquire(*args, **kwargs))


class _TailAcquire:
    def __init__(self, owner: _TailPool, inner: Any) -> None:
        self._owner = owner
        self._inner = inner

    async def __aenter__(self) -> Any:
        connection = await self._inner.__aenter__()
        self._owner.held += 1
        self._owner.most_held = max(self._owner.most_held, self._owner.held)
        return connection

    async def __aexit__(self, *exc_info: Any) -> Any:
        self._owner.held -= 1
        return await self._inner.__aexit__(*exc_info)


class _Tail:
    """One test's event tail: a small request pool, the LISTEN hub, and counters."""

    def __init__(self, pool: asyncpg.Pool, tail_pool: _TailPool, hub: Any) -> None:
        self.pool = pool
        self.tail_pool = tail_pool
        self.hub = hub
        self.reads: Counter[str] = Counter()
        self.reads_with_rows = 0
        self.reading = 0
        self.checks = 0
        self.rechecks = 0

    async def open(self, *, user_id: str, vault: str, vault_id: uuid.UUID, start: str | None = None) -> Any:
        response = await event_tail_service.open_tail(
            _Request(),  # type: ignore[arg-type]
            user_id=user_id,
            vault=vault,
            vault_id=vault_id,
            last_event_id=None,
            cursor=None,
            start=start,
            kinds=None,
        )
        return response.body_iterator


@contextlib.asynccontextmanager
async def _tail(
    monkeypatch,
    setup_pool: asyncpg.Pool,
    *,
    pool_size: int = _POOL_SIZE,
    budget: int = _BUDGET,
    heartbeat: float = 60.0,
) -> AsyncIterator[_Tail]:
    """Point the event tail at a small pool of its own on the test database.

    The access check is the real one, counted, and so is the batch read. Any
    checkout the access service makes for itself goes to the same small pool,
    so every database use of the tail is on the pool under test.
    """
    database = await setup_pool.fetchval("SELECT current_database()")
    pool = await asyncpg.create_pool(_database_dsn(database), min_size=0, max_size=pool_size)
    tail_pool = _TailPool(pool)
    hub = await _listening_hub(setup_pool)
    tail = _Tail(pool, tail_pool, hub)

    async def get_pool() -> _TailPool:
        return tail_pool

    async def counting_list_vault_events(conn: Any, vault_id: Any, **kwargs: Any) -> list[dict[str, Any]]:
        tail.reads[str(vault_id)] += 1
        tail.reading += 1
        try:
            rows = await list_vault_events(conn, vault_id, **kwargs)
        finally:
            tail.reading -= 1
        tail.reads_with_rows += bool(rows)
        return rows

    real_check_vault_access = access_service.check_vault_access

    async def counting_check_vault_access(*args: Any, **kwargs: Any) -> dict[str, Any]:
        tail.checks += 1
        return await real_check_vault_access(*args, **kwargs)

    # The check repeated for a reader slower than one check covers.
    real_recheck_access = getattr(event_tail_service, "_recheck_access", None)

    async def counting_recheck_access(**kwargs: Any) -> float:
        tail.rechecks += 1
        assert real_recheck_access is not None
        return await real_recheck_access(**kwargs)

    monkeypatch.setattr(settings, "system_hmac_secret", _CURSOR_SECRET)
    monkeypatch.setenv("AKB_EVENT_TAIL_DB_CONCURRENCY", str(budget))
    monkeypatch.setattr(event_tail_service, "_db_budget", None, raising=False)
    monkeypatch.setattr(event_tail_service, "_stream_slots", event_tail_service._StreamSlots())
    monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
    monkeypatch.setattr(access_service, "get_pool", get_pool)
    monkeypatch.setattr(event_tail_service, "get_event_tail_hub", lambda: hub)
    monkeypatch.setattr(event_tail_service, "list_vault_events", counting_list_vault_events)
    monkeypatch.setattr(event_tail_service, "check_vault_access", counting_check_vault_access)
    if real_recheck_access is not None:
        monkeypatch.setattr(event_tail_service, "_recheck_access", counting_recheck_access)
    monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", heartbeat)
    try:
        yield tail
    finally:
        await hub.close()
        await pool.close()


async def _make_reader(pool: asyncpg.Pool, name: str, vault_ids: list[uuid.UUID]) -> str:
    """A plain account with reader access to ``vault_ids``, as the real check reads it."""
    user_id = await pool.fetchval(
        "INSERT INTO users (username, email, password_hash) VALUES ($1, $2, 'unused') RETURNING id",
        name,
        f"{name}@example.test",
    )
    for vault_id in vault_ids:
        await pool.execute(
            "INSERT INTO vault_access (vault_id, user_id, role) VALUES ($1, $2, 'reader')",
            vault_id,
            user_id,
        )
    return str(user_id)


async def _revoke(pool: asyncpg.Pool, vault_id: uuid.UUID, user_id: str) -> None:
    await pool.execute(
        "DELETE FROM vault_access WHERE vault_id = $1 AND user_id = $2",
        vault_id,
        uuid.UUID(user_id),
    )


def _positions(frames: list[str]) -> list[int]:
    codec = EventCursorCodec(_CURSOR_SECRET)
    return [codec.decode(frame.splitlines()[1][len("id: ") :])[2] for frame in frames]


async def _changes(body: Any, expected: int) -> list[str]:
    """Read change frames from ``body`` until ``expected`` of them arrived."""
    frames: list[str] = []
    async for frame in body:
        if frame.startswith("event: change\n"):
            frames.append(frame)
            if len(frames) == expected:
                break
    return frames


async def _probe(pool: asyncpg.Pool, latencies: list[float], stop: asyncio.Event) -> None:
    """What the readiness probe does, every 100 ms: get a connection and ask it something."""
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        started = loop.time()
        try:
            async with pool.acquire(timeout=_PROBE_BUDGET_SECONDS) as conn:
                assert await conn.fetchval("SELECT 1") == 1
        except TimeoutError:
            latencies.append(math.inf)
        else:
            latencies.append(loop.time() - started)
        await asyncio.sleep(_PROBE_INTERVAL_SECONDS)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)]


async def _close(tasks: list[asyncio.Task[Any]], bodies: list[Any]) -> None:
    for task in tasks:
        task.cancel()
    # A generator can only be closed once its task has let go of it.
    await asyncio.gather(*tasks, return_exceptions=True)
    for body in bodies:
        await body.aclose()


async def test_a_burst_across_many_streams_leaves_the_pool_to_other_requests(monkeypatch):
    active_vaults, idle_vaults, readers, streams_per_vault = 6, 2, 4, 5
    # Each wave commits one event to every active Vault, back to back; the
    # pause between waves keeps the probe sampling across the whole burst.
    waves, wave_pause = 12, 0.15
    events_per_vault = waves
    async with _fresh_database() as setup_pool:
        names = [f"event-burst-{index}" for index in range(active_vaults + idle_vaults)]
        vault_ids = [await _make_vault(setup_pool, name) for name in names]
        users = [await _make_reader(setup_pool, f"burst-reader-{index}", vault_ids) for index in range(readers)]
        async with _tail(monkeypatch, setup_pool) as tail:
            bodies: list[Any] = []
            stream_vaults: list[int] = []
            for index in range(len(names) * streams_per_vault):
                vault = index % len(names)
                bodies.append(await tail.open(user_id=users[index % readers], vault=names[vault], vault_id=vault_ids[vault]))
                stream_vaults.append(vault)
            assert len(bodies) >= 32
            expected = [events_per_vault if vault < active_vaults else 0 for vault in stream_vaults]
            consumers = [
                asyncio.create_task(_changes(body, count)) for body, count in zip(bodies, expected) if count
            ]
            idle_bodies = [body for body, count in zip(bodies, expected) if not count]
            idle = [asyncio.create_task(anext(body)) for body in idle_bodies]
            latencies: list[float] = []
            stop_probing = asyncio.Event()
            probe = asyncio.create_task(_probe(tail.pool, latencies, stop_probing))
            inserted: dict[int, list[int]] = {vault: [] for vault in range(active_vaults)}
            try:
                # Every stream has subscribed, read its empty Vault, and waits.
                await _until(lambda: tail.hub.connected and tail.hub.subscriber_count == len(bodies))
                await _until(lambda: sum(tail.reads.values()) >= len(bodies) and tail.tail_pool.held == 0)

                for _ in range(waves):
                    for vault in range(active_vaults):
                        inserted[vault].append(
                            await _insert_event(setup_pool, vault_ids[vault], "document.put", "burst-writer")
                        )
                    await asyncio.sleep(wave_pause)
                delivered = await asyncio.wait_for(asyncio.gather(*consumers), timeout=30)
                # Keep probing a little after delivery, so the probe sees the tail settle.
                await asyncio.sleep(0.3)
            finally:
                stop_probing.set()
                await asyncio.gather(probe, return_exceptions=True)
                await _close([*consumers, *idle], bodies)

            assert probe.done() and probe.exception() is None
            peak = max(latencies)
            print(
                f"\nburst probe: n={len(latencies)} max={peak * 1000:.1f}ms "
                f"p99={_percentile(latencies, 0.99) * 1000:.1f}ms; "
                f"most tail connections held at once: {tail.tail_pool.most_held}; "
                f"reads={sum(tail.reads.values())} reads_with_rows={tail.reads_with_rows} "
                f"checks={tail.checks} rechecks={tail.rechecks}"
            )
            assert peak < _PROBE_BUDGET_SECONDS
            # No loss and no reordering: each stream got its Vault's events, in commit order.
            active_streams = [vault for vault in stream_vaults if vault < active_vaults]
            assert [_positions(frames) for frames in delivered] == [inserted[vault] for vault in active_streams]
            assert not any(task.done() and not task.cancelled() for task in idle)
            # Tail work never held more connections than its budget.
            assert tail.tail_pool.most_held <= _BUDGET
            # Every check either covered a batch it followed on the same
            # connection or renewed one for a slow reader: none per row, none
            # for a pass that found nothing.
            assert tail.checks == tail.reads_with_rows + tail.rechecks


async def test_streams_woken_while_the_database_stalls_leave_a_connection_for_requests(monkeypatch):
    """Woken streams whose reads stall must not take the connections requests need.

    Each woken stream used to hold a connection for its stalled read, so as
    many streams as the pool had connections took all of them, and a request
    waited out its whole deadline. On the previous code this test times out
    the probe.
    """
    streams = 12
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "event-stall")
        users = [await _make_reader(setup_pool, f"stall-reader-{index}", [vault_id]) for index in range(2)]
        async with _tail(monkeypatch, setup_pool) as tail:
            stalled = asyncio.Event()
            resume = asyncio.Event()
            counted_read = event_tail_service.list_vault_events

            async def stalling_read(conn: Any, vault: Any, **kwargs: Any) -> list[dict[str, Any]]:
                if stalled.is_set():
                    # The database stops answering, with the connection checked out.
                    await resume.wait()
                return await counted_read(conn, vault, **kwargs)

            monkeypatch.setattr(event_tail_service, "list_vault_events", stalling_read)
            bodies = [
                await tail.open(user_id=users[index % len(users)], vault="event-stall", vault_id=vault_id)
                for index in range(streams)
            ]
            consumers = [asyncio.create_task(_changes(body, 1)) for body in bodies]
            latencies: list[float] = []
            stop_probing = asyncio.Event()
            probe: asyncio.Task[None] | None = None
            try:
                await _until(lambda: tail.hub.connected and tail.hub.subscriber_count == streams)
                await _until(lambda: sum(tail.reads.values()) >= streams and tail.tail_pool.held == 0)

                stalled.set()
                event_id = await _insert_event(setup_pool, vault_id, "document.put", "stall-writer")
                # The woken streams' reads are stuck holding connections.
                await _until(lambda: tail.tail_pool.held >= _BUDGET)
                probe = asyncio.create_task(_probe(tail.pool, latencies, stop_probing))
                await asyncio.sleep(1.0)
                stop_probing.set()
                await probe
                held_while_stalled = tail.tail_pool.held

                resume.set()
                delivered = await asyncio.wait_for(asyncio.gather(*consumers), timeout=30)
            finally:
                stop_probing.set()
                resume.set()
                if probe is not None:
                    await asyncio.gather(probe, return_exceptions=True)
                await _close(consumers, bodies)

            print(
                f"\nstalled probe: n={len(latencies)} "
                f"max={max(latencies) * 1000:.1f}ms p99={_percentile(latencies, 0.99) * 1000:.1f}ms; "
                f"tail connections held while stalled: {held_while_stalled}"
            )
            assert latencies and max(latencies) < _PROBE_BUDGET_SECONDS, "a request could not get a connection"
            assert held_while_stalled <= _BUDGET
            assert tail.tail_pool.most_held <= _BUDGET
            # Nothing was lost to the stall: every stream got the event once the database answered.
            assert [_positions(frames) for frames in delivered] == [[event_id]] * streams


async def test_access_revoked_between_the_read_and_the_check_sends_none_of_the_rows(monkeypatch):
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "event-revoke-read")
        user_id = await _make_reader(setup_pool, "revoke-read-reader", [vault_id])
        for kind in ("document.put", "document.update", "document.delete"):
            await _insert_event(setup_pool, vault_id, kind, "alice")
        async with _tail(monkeypatch, setup_pool) as tail:
            counted_read = event_tail_service.list_vault_events

            async def read_then_revoke(conn: Any, vault: Any, **kwargs: Any) -> list[dict[str, Any]]:
                rows = await counted_read(conn, vault, **kwargs)
                if rows:
                    # Committed after the rows were read and before the check runs.
                    await _revoke(setup_pool, vault_id, user_id)
                return rows

            monkeypatch.setattr(event_tail_service, "list_vault_events", read_then_revoke)
            body = await tail.open(user_id=user_id, vault="event-revoke-read", vault_id=vault_id, start="earliest")
            frames = [frame async for frame in body]

            assert frames == []
            assert tail.checks == 1
            assert tail.tail_pool.held == 0


async def test_a_slow_reader_is_checked_again_before_its_next_row(monkeypatch):
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "event-revoke-slow")
        user_id = await _make_reader(setup_pool, "revoke-slow-reader", [vault_id])
        event_ids = [await _insert_event(setup_pool, vault_id, "document.put", "alice") for _ in range(3)]
        async with _tail(monkeypatch, setup_pool) as tail:
            # A reader that keeps up is sent the whole batch on one check.
            body = await tail.open(user_id=user_id, vault="event-revoke-slow", vault_id=vault_id, start="earliest")
            assert _positions([await anext(body) for _ in event_ids]) == event_ids
            await body.aclose()
            assert tail.checks == 1

            tail.checks = 0
            body = await tail.open(user_id=user_id, vault="event-revoke-slow", vault_id=vault_id, start="earliest")
            first = await anext(body)
            await _revoke(setup_pool, vault_id, user_id)
            # Slower than the window one check covers: the next row waits for a new check.
            await asyncio.sleep(event_tail_service._RECHECK_DURING_YIELD_SECONDS + 0.2)
            rest = [frame async for frame in body]

            assert _positions([first]) == event_ids[:1]
            assert rest == []
            assert tail.checks == 2
            assert event_tail_service._RECHECK_DURING_YIELD_SECONDS == 1.0


async def test_a_stream_without_rows_closes_within_one_heartbeat_of_losing_access(monkeypatch):
    heartbeat = 0.4
    async with _fresh_database() as setup_pool:
        vault_id = await _make_vault(setup_pool, "event-revoke-idle")
        user_id = await _make_reader(setup_pool, "revoke-idle-reader", [vault_id])
        async with _tail(monkeypatch, setup_pool, heartbeat=heartbeat) as tail:
            loop = asyncio.get_running_loop()
            body = await tail.open(user_id=user_id, vault="event-revoke-idle", vault_id=vault_id)
            frames: list[tuple[float, str]] = []

            async def read_all() -> None:
                async for frame in body:
                    frames.append((loop.time(), frame))

            reader = asyncio.create_task(read_all())
            try:
                await _until(lambda: tail.hub.connected and tail.hub.subscriber_count == 1)
                await _until(lambda: tail.reads[str(vault_id)] >= 1 and tail.reading == 0)
                # Wake-ups that find nothing cost a read each and no access
                # check. Only a heartbeat's pass checks, and it does so after
                # the heartbeat is sent, so checks never outnumber heartbeats.
                for _ in range(3):
                    reads = tail.reads[str(vault_id)]
                    await setup_pool.execute("SELECT pg_notify('akb_events', '0')")
                    await _until(lambda: tail.reads[str(vault_id)] > reads and tail.reading == 0)
                heartbeats_before = sum(1 for _, frame in frames if frame == format_heartbeat())
                assert tail.checks <= heartbeats_before

                revoked_at = loop.time()
                await _revoke(setup_pool, vault_id, user_id)
                done, _ = await asyncio.wait({reader}, timeout=4 * heartbeat)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await body.aclose()

            assert reader in done, "a stream that lost access stayed open past its heartbeat"
            closed_after = loop.time() - revoked_at
            after_revocation = [frame for at, frame in frames if at >= revoked_at]
            print(f"\nidle stream closed {closed_after * 1000:.0f}ms after revocation (heartbeat {heartbeat * 1000:.0f}ms)")
            # At most the heartbeat whose pass found the revocation, then the end.
            assert after_revocation in ([], [format_heartbeat()])
            assert closed_after < 2 * heartbeat
