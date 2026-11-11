"""Unit contracts for the process-wide event tail LISTEN hub.

The fake connection mirrors the asyncpg behaviour the hub relies on: listener
and termination callbacks are delivered with ``loop.call_soon``, a closed
connection forgets its listeners, and termination callbacks fire once on any
close, whether the server dropped it or the client closed it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextvars

import pytest

from app.services import event_tail_hub
from app.services.event_tail_hub import EVENT_CHANNEL, EventTailHub


pytestmark = pytest.mark.asyncio

_real_sleep = asyncio.sleep


class _FakeListenConnection:
    def __init__(self, *, fail_keepalive: bool = False) -> None:
        self.listeners: dict[str, list[Callable[..., object]]] = {}
        self.termination_listeners: list[Callable[[object], object]] = []
        self.fail_keepalive = fail_keepalive
        self.closed = False
        self.close_calls = 0
        self.terminate_calls = 0
        self.keepalives = 0

    def add_termination_listener(self, callback: Callable[[object], object]) -> None:
        self.termination_listeners.append(callback)

    async def add_listener(self, channel: str, callback: Callable[..., object]) -> None:
        self.listeners.setdefault(channel, []).append(callback)

    async def fetchval(self, query: str, *, timeout: float | None = None) -> int:
        assert query == "SELECT 1"
        self.keepalives += 1
        if self.fail_keepalive:
            raise TimeoutError
        return 1

    def is_closed(self) -> bool:
        return self.closed

    async def close(self, *, timeout: float | None = None) -> None:
        self.close_calls += 1
        self._drop()

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._drop()

    def notify(self, payload: str = "1") -> None:
        loop = asyncio.get_running_loop()
        for callback in self.listeners.get(EVENT_CHANNEL, []):
            loop.call_soon(callback, self, 4242, EVENT_CHANNEL, payload)

    def lose(self) -> None:
        """The server went away without the client asking."""
        self._drop()

    def _drop(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.listeners.clear()
        callbacks, self.termination_listeners = self.termination_listeners, []
        loop = asyncio.get_running_loop()
        for callback in callbacks:
            loop.call_soon(callback, self)


class _Connector:
    """Connect factory: hands out queued connections or raises queued errors."""

    def __init__(self, *outcomes: _FakeListenConnection | BaseException) -> None:
        self._outcomes = list(outcomes)
        self.connections: list[_FakeListenConnection] = []
        self.calls = 0

    async def __call__(self) -> _FakeListenConnection:
        self.calls += 1
        outcome = self._outcomes.pop(0) if self._outcomes else _FakeListenConnection()
        if isinstance(outcome, BaseException):
            raise outcome
        self.connections.append(outcome)
        return outcome


async def _until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await _real_sleep(0.001)

    await asyncio.wait_for(poll(), timeout)


async def _settle(iterations: int = 5) -> None:
    for _ in range(iterations):
        await _real_sleep(0)


def _hub(connector: _Connector, **overrides: float) -> EventTailHub:
    options: dict[str, float] = {
        "reconnect_initial_delay": 0.0,
        "reconnect_max_delay": 0.0,
        "keepalive_interval": 60.0,
        "close_timeout": 1.0,
    }
    options.update(overrides)
    return EventTailHub(connector, **options)


async def test_first_subscribe_starts_one_listener_and_a_notification_wakes_every_subscriber() -> None:
    connector = _Connector()
    hub = _hub(connector)
    try:
        assert not hub.connected
        first = hub.subscribe()
        second = hub.subscribe()
        await _until(lambda: hub.connected)

        assert connector.calls == 1
        connection = connector.connections[0]
        assert len(connection.listeners[EVENT_CHANNEL]) == 1
        assert hub.subscriber_count == 2
        # Establishing LISTEN wakes everyone: anything committed before it
        # notified nobody.
        await _until(lambda: first.wakeup.is_set() and second.wakeup.is_set())

        first.wakeup.clear()
        second.wakeup.clear()
        connection.notify()
        await _until(lambda: first.wakeup.is_set() and second.wakeup.is_set())
    finally:
        await hub.close()


async def test_unsubscribed_stream_is_no_longer_woken() -> None:
    connector = _Connector()
    hub = _hub(connector)
    try:
        kept = hub.subscribe()
        dropped = hub.subscribe()
        await _until(lambda: hub.connected)
        await _settle()
        kept.wakeup.clear()
        dropped.wakeup.clear()

        hub.unsubscribe(dropped)
        hub.unsubscribe(dropped)  # repeated calls are harmless
        connector.connections[0].notify()
        await _until(kept.wakeup.is_set)
        await _settle()

        assert not dropped.wakeup.is_set()
        assert hub.subscriber_count == 1
    finally:
        await hub.close()


async def test_a_burst_of_notifications_is_one_fan_out_pass() -> None:
    connector = _Connector()
    hub = _hub(connector)
    try:
        subscription = hub.subscribe()
        await _until(lambda: hub.connected)
        await _settle()
        subscription.wakeup.clear()
        passes = 0
        flush = hub._flush_wakeups

        def counting_flush() -> None:
            nonlocal passes
            passes += 1
            flush()

        hub._flush_wakeups = counting_flush  # type: ignore[method-assign]
        for event_id in range(50):
            connector.connections[0].notify(str(event_id))
        await _until(subscription.wakeup.is_set)
        await _settle()

        assert passes == 1
    finally:
        await hub.close()


async def test_lost_connection_wakes_subscribers_and_listens_again() -> None:
    first_connection = _FakeListenConnection()
    second_connection = _FakeListenConnection()
    connector = _Connector(first_connection, second_connection)
    hub = _hub(connector, reconnect_initial_delay=0.01, reconnect_max_delay=0.01)
    try:
        subscription = hub.subscribe()
        await _until(lambda: hub.connected)
        await _settle()
        subscription.wakeup.clear()

        first_connection.lose()
        # The stream is woken to re-read: notifications sent while nothing
        # listens are gone for good.
        await _until(subscription.wakeup.is_set)
        await _until(lambda: connector.calls == 2 and hub.connected)
        assert len(second_connection.listeners[EVENT_CHANNEL]) == 1

        await _settle()
        subscription.wakeup.clear()
        second_connection.notify()
        await _until(subscription.wakeup.is_set)
    finally:
        await hub.close()


async def test_failed_connects_retry_with_bounded_exponential_backoff(monkeypatch) -> None:
    sleeps: list[float] = []

    async def recording_sleep(delay: float, result: object = None) -> object:
        sleeps.append(delay)
        await _real_sleep(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    connector = _Connector(
        OSError("connection refused"),
        OSError("connection refused"),
        OSError("connection refused"),
    )
    hub = _hub(connector, reconnect_initial_delay=0.5, reconnect_max_delay=1.0)
    try:
        subscription = hub.subscribe()  # never raises, even though connecting fails
        await _until(lambda: hub.connected)
        await _until(subscription.wakeup.is_set)

        assert connector.calls == 4
        assert sleeps == [0.5, 1.0, 1.0]
    finally:
        await hub.close()


async def test_keepalive_failure_drops_a_silent_connection_and_reconnects() -> None:
    silent = _FakeListenConnection(fail_keepalive=True)
    healthy = _FakeListenConnection()
    connector = _Connector(silent, healthy)
    hub = _hub(connector, keepalive_interval=0.01)
    try:
        hub.subscribe()
        await _until(lambda: connector.calls == 2 and hub.connected)

        assert silent.keepalives >= 1
        assert silent.terminate_calls == 1
        assert silent.closed
        await _until(lambda: healthy.keepalives >= 1)
    finally:
        await hub.close()


async def test_close_stops_listening_closes_gracefully_and_wakes_subscribers() -> None:
    connector = _Connector()
    hub = _hub(connector)
    subscription = hub.subscribe()
    await _until(lambda: hub.connected)
    await _settle()
    subscription.wakeup.clear()
    connection = connector.connections[0]

    await hub.close()

    assert not hub.connected
    assert connection.closed
    assert connection.close_calls == 1
    assert connection.terminate_calls == 0
    assert subscription.wakeup.is_set()

    # A closed hub never reconnects; a late subscriber relies on its heartbeat.
    late = hub.subscribe()
    await _settle()
    assert connector.calls == 1
    assert not hub.connected
    hub.unsubscribe(late)
    await hub.close()


async def test_close_without_subscribers_is_a_no_op() -> None:
    connector = _Connector()
    hub = _hub(connector)

    await hub.close()
    await hub.close()

    assert connector.calls == 0
    assert not hub.connected


async def test_listener_task_does_not_inherit_the_subscribing_request_context() -> None:
    request_scoped: contextvars.ContextVar[str] = contextvars.ContextVar("request_scoped", default="unset")
    seen: list[str] = []
    connector = _Connector()

    async def connect() -> _FakeListenConnection:
        seen.append(request_scoped.get())
        return await connector()

    hub = EventTailHub(connect, reconnect_initial_delay=0.0, close_timeout=1.0)
    token = request_scoped.set("request-1")
    try:
        hub.subscribe()
        await _until(lambda: hub.connected)
    finally:
        request_scoped.reset(token)
        await hub.close()

    assert seen == ["unset"]


async def test_close_event_tail_hub_closes_the_process_hub_and_clears_the_handle(monkeypatch) -> None:
    monkeypatch.setattr(event_tail_hub, "_hub", None)
    created = event_tail_hub.get_event_tail_hub()
    assert event_tail_hub.get_event_tail_hub() is created
    await event_tail_hub.close_event_tail_hub()

    connector = _Connector()
    running = _hub(connector)
    monkeypatch.setattr(event_tail_hub, "_hub", running)
    running.subscribe()
    await _until(lambda: running.connected)

    await event_tail_hub.close_event_tail_hub()
    await event_tail_hub.close_event_tail_hub()

    assert not running.connected
    assert connector.connections[0].close_calls == 1
    replacement = event_tail_hub.get_event_tail_hub()
    assert replacement is not running
    await event_tail_hub.close_event_tail_hub()


async def test_storage_shutdown_closes_the_hub_before_the_pool(monkeypatch, tmp_path) -> None:
    import types

    from app.config import settings

    # Keep any import-time storage setup off /data, as the lifecycle tests do.
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.services import lifecycle

    calls: list[str] = []

    def recorder(name: str) -> Callable[[], object]:
        async def record() -> None:
            calls.append(name)

        return record

    monkeypatch.setattr(lifecycle, "settings", types.SimpleNamespace(keycloak_enabled=False))
    monkeypatch.setattr(lifecycle.http_pool, "close_client", recorder("http_client"))
    monkeypatch.setattr(lifecycle, "close_event_tail_hub", recorder("event_tail_hub"))
    monkeypatch.setattr(lifecycle, "close_pool", recorder("pool"))

    await lifecycle.shutdown_storage()

    assert calls == ["http_client", "event_tail_hub", "pool"]
