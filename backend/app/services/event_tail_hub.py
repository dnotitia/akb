"""One PostgreSQL LISTEN connection shared by every Vault Change Event Tail stream.

A stream used to LISTEN on a connection it took from the request pool and kept
for its whole life. Streams are long-lived, and a client may open one per
Vault, so a single client could hold every pooled connection. Requests,
including the readiness probe, then waited for a connection that never came
back, while the database itself sat idle.

This module keeps one connection per process, opened outside the request pool,
that LISTENs on the channel the ``events`` insert trigger notifies. A
notification carries only the new event's id, not its Vault, so every
subscriber is woken and re-reads its own Vault from the table by cursor. The
table is the source of truth and a notification is only a hint: a wake-up that
never arrives delays a stream until its next heartbeat re-read and loses
nothing. That is why the hub never raises into a stream. A failed or lost
connection wakes every subscriber and is re-established with bounded backoff.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import contextvars
import logging
from typing import Any

import asyncpg

from app.config import settings

logger = logging.getLogger("akb.event_tail_hub")

# The channel the `events` AFTER INSERT trigger notifies (migration 015); the
# payload is the new row's id.
EVENT_CHANNEL = "akb_events"

_CONNECT_TIMEOUT_SECONDS = 10.0
# Bounds LISTEN itself; the keepalive passes its own, shorter timeout.
_COMMAND_TIMEOUT_SECONDS = 10.0
_RECONNECT_INITIAL_DELAY_SECONDS = 0.5
_RECONNECT_MAX_DELAY_SECONDS = 30.0
_KEEPALIVE_INTERVAL_SECONDS = 30.0
_KEEPALIVE_TIMEOUT_SECONDS = 5.0
# A loss restarts the reconnect backoff only if LISTEN had been held this
# long: one keepalive interval. A connection lost sooner counts as one more
# failed attempt, so a server that drops each new connection right after
# LISTEN is retried at growing intervals rather than every initial delay.
_RECONNECT_RESET_AFTER_SECONDS = _KEEPALIVE_INTERVAL_SECONDS
_CLOSE_TIMEOUT_SECONDS = 5.0

ListenerConnect = Callable[[], Awaitable[Any]]


async def _connect_listener() -> asyncpg.Connection:
    """Open the hub's own connection. It is never taken from the request pool."""
    return await asyncpg.connect(
        settings.asyncpg_dsn,
        timeout=_CONNECT_TIMEOUT_SECONDS,
        command_timeout=_COMMAND_TIMEOUT_SECONDS,
        server_settings={"application_name": "akb-event-tail-listener"},
    )


class EventTailSubscription:
    """One stream's wake-up flag: the hub sets it, the stream clears and awaits it."""

    __slots__ = ("wakeup",)

    def __init__(self) -> None:
        self.wakeup = asyncio.Event()


class EventTailHub:
    """Fan one LISTEN connection's notifications out to every subscribed stream."""

    def __init__(
        self,
        connect: ListenerConnect | None = None,
        *,
        channel: str = EVENT_CHANNEL,
        reconnect_initial_delay: float = _RECONNECT_INITIAL_DELAY_SECONDS,
        reconnect_max_delay: float = _RECONNECT_MAX_DELAY_SECONDS,
        reconnect_reset_after: float = _RECONNECT_RESET_AFTER_SECONDS,
        keepalive_interval: float = _KEEPALIVE_INTERVAL_SECONDS,
        keepalive_timeout: float = _KEEPALIVE_TIMEOUT_SECONDS,
        close_timeout: float = _CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._connect = connect or _connect_listener
        self._channel = channel
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._reconnect_reset_after = reconnect_reset_after
        self._keepalive_interval = keepalive_interval
        self._keepalive_timeout = keepalive_timeout
        self._close_timeout = close_timeout
        self._subscribers: set[EventTailSubscription] = set()
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._closed = False
        self._wake_pending = False

    @property
    def connected(self) -> bool:
        """True while the dedicated connection is open and LISTENing."""
        return self._connected

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> EventTailSubscription:
        """Register a wake-up flag, starting the listener on first use.

        Call from the event loop. A database problem never surfaces here: the
        connection is opened in the background, and until it is up a stream
        still re-reads on its heartbeat.
        """
        subscription = EventTailSubscription()
        self._subscribers.add(subscription)
        self._ensure_running()
        return subscription

    def unsubscribe(self, subscription: EventTailSubscription) -> None:
        """Stop waking ``subscription``. Unknown or repeated calls are no-ops."""
        self._subscribers.discard(subscription)

    async def close(self) -> None:
        """Stop listening and close the connection. Safe to call more than once.

        Streams still subscribed are woken once and then keep working on their
        heartbeat re-read alone.
        """
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            # The task closes its own connection on the way out, gracefully,
            # within close_timeout; allow for that plus a margin.
            _, pending = await asyncio.wait({task}, timeout=self._close_timeout * 2)
            if pending:
                logger.warning("Event tail listener did not stop within %.1fs", self._close_timeout * 2)
        self._connected = False
        self._wake_pending = False
        for subscription in tuple(self._subscribers):
            subscription.wakeup.set()

    def _ensure_running(self) -> None:
        if self._closed:
            return
        if self._task is not None and not self._task.done():
            return
        # A wake-up scheduled on a loop that is gone would never clear the flag.
        self._wake_pending = False
        # A fresh context: the listener outlives the request that started it and
        # must not carry that request's context variables.
        self._task = asyncio.get_running_loop().create_task(
            self._run(),
            name="event-tail-listener",
            context=contextvars.Context(),
        )

    async def _run(self) -> None:
        delay = self._reconnect_initial_delay
        while not self._closed:
            try:
                held = await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a listener failure must never reach a stream
                if self._closed:
                    return
                logger.warning(
                    "Event tail listener unavailable (%s: %s); retrying in %.1fs",
                    type(exc).__name__,
                    exc,
                    delay,
                )
                wait = delay
                delay = min(delay * 2, self._reconnect_max_delay)
            else:
                if self._closed:
                    return
                if held >= self._reconnect_reset_after:
                    # It stayed up: the next failure starts from the bottom of the backoff.
                    delay = self._reconnect_initial_delay
                    wait = delay
                else:
                    # Lost soon after LISTEN: one more failed attempt.
                    wait = delay
                    delay = min(delay * 2, self._reconnect_max_delay)
                logger.warning(
                    "Event tail listener connection lost after %.1fs; reconnecting in %.1fs",
                    held,
                    wait,
                )
            # Notifications sent while nothing listened are gone. Wake every
            # stream so it re-reads by cursor now, not at its next heartbeat.
            self._wake_all()
            await asyncio.sleep(wait)

    async def _listen(self) -> float:
        """Hold one connection until it is lost or the hub closes.

        Raises only while establishing it. Once LISTEN succeeds, a loss or a
        failed keepalive returns normally, with how long LISTEN was held.
        """
        connection = await self._connect()
        lost = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_terminate(_connection: object) -> None:
            # Only this connection's own flag: asyncpg delivers the callback a
            # loop iteration later, by which time a successor may be up.
            lost.set()

        try:
            connection.add_termination_listener(on_terminate)
            await connection.add_listener(self._channel, self._on_notify)
            self._connected = True
            listening_since = loop.time()
            logger.info("Event tail listener connected")
            # Anything committed before LISTEN took effect notified nobody.
            self._wake_all()
            await self._watch(connection, lost)
            return loop.time() - listening_since
        finally:
            self._connected = False
            if not connection.is_closed():
                if self._closed:
                    # Shutdown: say goodbye to the server. asyncpg aborts the
                    # connection itself if this fails or is cancelled.
                    with contextlib.suppress(Exception):
                        await connection.close(timeout=self._close_timeout)
                else:
                    connection.terminate()

    async def _watch(self, connection: Any, lost: asyncio.Event) -> None:
        """Return once the connection is lost or stops answering."""
        while not self._closed:
            try:
                await asyncio.wait_for(lost.wait(), timeout=self._keepalive_interval)
            except TimeoutError:
                pass
            else:
                return
            # A connection can go half-open without the client noticing, for
            # example an idle flow dropped by a NAT or load balancer. Asking it
            # something detects that, and keeps an idle flow from being dropped.
            try:
                await connection.fetchval("SELECT 1", timeout=self._keepalive_timeout)
            except Exception as exc:  # noqa: BLE001 - any failure means reconnect
                logger.warning("Event tail listener keepalive failed (%s)", type(exc).__name__)
                return

    def _on_notify(self, *_args: object) -> None:
        self._wake_all()

    def _wake_all(self) -> None:
        """Schedule one wake-up of every subscriber; a burst shares one pass."""
        if self._wake_pending:
            return
        self._wake_pending = True
        asyncio.get_running_loop().call_soon(self._flush_wakeups)

    def _flush_wakeups(self) -> None:
        self._wake_pending = False
        for subscription in tuple(self._subscribers):
            subscription.wakeup.set()


_hub: EventTailHub | None = None


def get_event_tail_hub() -> EventTailHub:
    """The process-wide hub, created on first use. It connects on first subscribe."""
    global _hub
    if _hub is None:
        _hub = EventTailHub()
    return _hub


def event_tail_hub_connected() -> bool:
    """Whether the process-wide hub is LISTENing now. Never creates the hub."""
    return _hub is not None and _hub.connected


async def close_event_tail_hub() -> None:
    """Close the process-wide hub and clear the handle, as `close_pool()` does."""
    global _hub
    if _hub is not None:
        hub, _hub = _hub, None
        await hub.close()
