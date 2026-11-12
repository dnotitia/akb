"""PostgreSQL proof for the event tail LISTEN hub's own connection.

The hub must open its connection from the configured DSN rather than borrow
one from the request pool, wake on a committed event, and recover when the
server drops that connection. While it cannot get that connection back, an
open stream must still find new rows on its own heartbeat re-read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from app.config import settings
from app.services import event_tail_service
from app.services.event_tail_hub import EventTailHub, EventTailSubscription
from app.services.event_tail_service import EventCursorCodec, format_heartbeat
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _insert_event, _make_vault


pytestmark = pytest.mark.asyncio

_LISTENER_APPLICATION_NAME = "akb-event-tail-listener"
# Stands in for the 15 s production interval so a heartbeat re-read happens
# within the test.
_HEARTBEAT_SECONDS = 0.5
_REFUSED_RECONNECTS = 2


async def _until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def _point_settings_at(monkeypatch, dsn: str) -> None:
    parts = urlsplit(dsn)
    monkeypatch.setattr(settings, "db_host", parts.hostname)
    monkeypatch.setattr(settings, "db_port", parts.port or 5432)
    monkeypatch.setattr(settings, "db_user", unquote(parts.username or ""))
    monkeypatch.setattr(settings, "db_password", unquote(parts.password or ""))
    monkeypatch.setattr(settings, "db_name", parts.path.lstrip("/"))


async def _listener_pids(pool, database: str) -> list[int]:
    rows = await pool.fetch(
        "SELECT pid FROM pg_stat_activity WHERE application_name = $1 AND datname = $2",
        _LISTENER_APPLICATION_NAME,
        database,
    )
    return [row["pid"] for row in rows]


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _ListenerThatCannotReconnect:
    """The hub's connect factory: one real connection, and no way back once it drops.

    The first reconnects are refused at once, and the hub wakes every stream
    after each refusal. The next one hangs, as a connect to an unreachable
    server does until it times out, so from then on the hub has nothing to wake
    a stream with. Closing the hub cancels it.
    """

    def __init__(self, dsn: str, *, refusals: int) -> None:
        self._dsn = dsn
        self._refusals = refusals
        self.connection: asyncpg.Connection | None = None
        self.reconnects = 0
        self.hanging = asyncio.Event()

    async def __call__(self) -> Any:
        if self.connection is None:
            self.connection = await asyncpg.connect(self._dsn)
            return self.connection
        self.reconnects += 1
        if self.reconnects <= self._refusals:
            raise ConnectionRefusedError("the listener's reconnect was refused")
        self.hanging.set()
        # Nothing resolves this future.
        return await asyncio.get_running_loop().create_future()


class _CountedWakeup(asyncio.Event):
    """A stream's wake-up flag that counts how often the hub raised it."""

    def __init__(self) -> None:
        super().__init__()
        self.raised = 0

    def set(self) -> None:
        self.raised += 1
        super().set()


async def test_hub_listens_on_its_own_connection_and_recovers_after_the_server_drops_it(monkeypatch):
    async with _fresh_database() as pool:
        database = await pool.fetchval("SELECT current_database()")
        _point_settings_at(monkeypatch, _database_dsn(database))
        vault_id = await _make_vault(pool, "event-hub")
        pool_size = pool.get_size()
        hub = EventTailHub(reconnect_initial_delay=0.05, close_timeout=2.0)
        try:
            subscription = hub.subscribe(vault_id)
            await _until(lambda: hub.connected)
            pids = await _listener_pids(pool, database)
            assert len(pids) == 1
            # The listener is not a request-pool connection.
            assert pool.get_size() == pool_size

            await asyncio.sleep(0.05)
            subscription.wakeup.clear()
            await _insert_event(pool, vault_id, "document.put", "alice")
            await asyncio.wait_for(subscription.wakeup.wait(), 5)

            subscription.wakeup.clear()
            assert await pool.fetchval("SELECT pg_terminate_backend($1)", pids[0])
            # Losing the connection wakes the stream so it re-reads by cursor.
            await asyncio.wait_for(subscription.wakeup.wait(), 5)
            await _until(lambda: hub.connected)
            replacement = await _listener_pids(pool, database)
            assert len(replacement) == 1
            assert replacement != pids

            await asyncio.sleep(0.05)
            subscription.wakeup.clear()
            await _insert_event(pool, vault_id, "document.update", "alice")
            await asyncio.wait_for(subscription.wakeup.wait(), 5)
        finally:
            await hub.close()

        assert not hub.connected
        for _ in range(100):
            if not await _listener_pids(pool, database):
                break
            await asyncio.sleep(0.05)
        assert await _listener_pids(pool, database) == []


async def test_stream_reads_a_commit_on_its_heartbeat_while_the_listener_cannot_reconnect(monkeypatch):
    """A row committed while nothing LISTENs still reaches an open stream.

    The commit's NOTIFY goes nowhere and the hub, unable to reconnect, wakes no
    stream, so the stream's own heartbeat re-read is the only way to the row.
    """
    async with _fresh_database() as pool:
        database = await pool.fetchval("SELECT current_database()")
        vault_id = await _make_vault(pool, "event-hub-down")
        connector = _ListenerThatCannotReconnect(_database_dsn(database), refusals=_REFUSED_RECONNECTS)
        hub = EventTailHub(connector, reconnect_initial_delay=0.01, reconnect_max_delay=0.01, close_timeout=2.0)
        wakeups: list[_CountedWakeup] = []
        subscribe = hub.subscribe

        def subscribe_counted(vault: object) -> EventTailSubscription:
            subscription = subscribe(vault)
            subscription.wakeup = _CountedWakeup()
            wakeups.append(subscription.wakeup)
            return subscription

        reading = 0
        read_pass = event_tail_service._read_pass

        async def tracked_read_pass(**kwargs: Any) -> tuple[list[dict[str, Any]], float | None]:
            nonlocal reading
            reading += 1
            try:
                return await read_pass(**kwargs)
            finally:
                reading -= 1

        async def get_pool() -> asyncpg.Pool:
            return pool

        async def check_vault_access(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"vault_id": vault_id}

        monkeypatch.setattr(hub, "subscribe", subscribe_counted)
        monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
        monkeypatch.setattr(event_tail_service, "get_event_tail_hub", lambda: hub)
        monkeypatch.setattr(event_tail_service, "_read_pass", tracked_read_pass)
        monkeypatch.setattr(event_tail_service, "check_vault_access", check_vault_access)
        monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", _HEARTBEAT_SECONDS)
        codec = EventCursorCodec("cursor-secret")
        stream = event_tail_service._stream_events(
            _Request(),
            user_id="user",
            vault="event-hub-down",
            vault_id=vault_id,
            kinds=(),
            position=0,
            codec=codec,
        )
        frames: list[str] = []

        async def read_until_a_change() -> None:
            async for frame in stream:
                frames.append(frame)
                if frame.startswith("event: change\n"):
                    return

        reader = asyncio.create_task(read_until_a_change())
        try:
            await _until(lambda: len(wakeups) == 1)
            (wakeup,) = wakeups
            # Establishing LISTEN wakes the stream once.
            await _until(lambda: hub.connected and wakeup.raised > 0)
            raised_before_loss = wakeup.raised
            assert connector.connection is not None
            assert await pool.fetchval("SELECT pg_terminate_backend($1)", connector.connection.get_server_pid())

            # The loss wakes the stream, and so does each refused reconnect.
            # Once a reconnect hangs, the hub has nothing left to wake it with.
            await asyncio.wait_for(connector.hanging.wait(), 5)
            assert wakeup.raised > raised_before_loss
            # Commit only once the stream has handled every wake-up and waits
            # again, so no read that a wake-up started can find the row.
            await _until(lambda: not wakeup.is_set() and reading == 0)
            raised_before_commit = wakeup.raised
            frames_before_commit = len(frames)
            event_id = await _insert_event(pool, vault_id, "document.put", "alice")

            # The stream's next heartbeat re-read starts at most one interval
            # after the commit. The rest of the allowance is slack for a loaded
            # event loop or a slow read; nothing else can deliver the row, so a
            # longer wait cannot let a stream that ignores its heartbeat pass.
            done, _ = await asyncio.wait({reader}, timeout=6 * _HEARTBEAT_SECONDS)
            assert reader in done, "a row committed while the listener was down never reached the stream"
            reader.result()
            # Nothing woke the stream, and the hub is still without a connection.
            assert wakeup.raised == raised_before_commit
            assert not hub.connected
            assert connector.reconnects == _REFUSED_RECONNECTS + 1
        finally:
            reader.cancel()
            # A generator can only be closed once its task has let go of it.
            await asyncio.gather(reader, return_exceptions=True)
            await stream.aclose()
            await hub.close()

        assert hub.subscriber_count == 0
        *heartbeats, change = frames[frames_before_commit:]
        # A heartbeat ended the wait, and the re-read after it found the row.
        assert heartbeats
        assert set(heartbeats) == {format_heartbeat()}
        assert change.startswith("event: change\n")
        assert codec.decode(change.splitlines()[1][4:])[2] == event_id
