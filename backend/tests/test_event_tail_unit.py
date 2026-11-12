"""Unit contracts for the PostgreSQL-backed event tail."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
import gc
import json
import sys
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID
import weakref

import pytest
from pydantic import ValidationError

from app.services.event_tail_hub import EventTailSubscription
from app.services.event_tail_service import (
    EventBounds,
    EventCursorCodec,
    EventCursorError,
    EventGapError,
    format_heartbeat,
    format_sse,
    normalize_kind_filter,
    resolve_start_position,
    validate_event_gap,
)


VAULT_ID = UUID("11111111-1111-4111-8111-111111111111")


class _FakeHub:
    """Stands in for the process-wide LISTEN hub."""

    def __init__(self) -> None:
        self.subscriptions: list[EventTailSubscription] = []
        self.unsubscribed: list[EventTailSubscription] = []

    def subscribe(self, vault_id: object) -> EventTailSubscription:
        subscription = EventTailSubscription(str(vault_id))
        self.subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: EventTailSubscription) -> None:
        self.subscriptions.remove(subscription)
        self.unsubscribed.append(subscription)

    def notify(self) -> None:
        for subscription in self.subscriptions:
            subscription.wakeup.set()


class _Pool:
    """A pool double that bounds and counts checked-out connections."""

    def __init__(self, connection: object, *, max_size: int = 30) -> None:
        self._connection = connection
        self._available = asyncio.Semaphore(max_size)
        self.checked_out = 0
        self.acquisitions = 0
        self.release_timeouts: list[float | None] = []

    def acquire(self) -> "_Acquire":
        return _Acquire(self)

    async def release(self, _connection: object, *, timeout: float | None = None) -> None:
        self.release_timeouts.append(timeout)
        self.checked_out -= 1
        self._available.release()


class _Acquire:
    """Like asyncpg's checkout: awaited for a connection, or used as a context manager."""

    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    def __await__(self) -> Any:
        return self.__aenter__().__await__()

    async def __aenter__(self) -> object:
        await self._pool._available.acquire()
        self._pool.checked_out += 1
        self._pool.acquisitions += 1
        return self._pool._connection

    async def __aexit__(self, *_exc: object) -> None:
        self._pool.checked_out -= 1
        self._pool._available.release()


@pytest.fixture
def hub(monkeypatch) -> _FakeHub:
    from app.services import event_tail_service

    fake = _FakeHub()
    monkeypatch.setattr(event_tail_service, "get_event_tail_hub", lambda: fake)
    return fake


def _stream_test_doubles(*, max_size: int = 30) -> tuple[_Pool, MagicMock, MagicMock]:
    connection = MagicMock()
    pool = _Pool(connection, max_size=max_size)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    return pool, connection, request


def test_cursor_is_opaque_authenticated_and_scope_bound() -> None:
    codec = EventCursorCodec("cursor-secret")

    token = codec.encode(VAULT_ID, ("document.put", "table.rows_changed"), 42)

    assert token.startswith("ec1.")
    assert "42" not in token
    assert str(VAULT_ID) not in token
    assert "document.put" not in token
    assert codec.decode(token) == (VAULT_ID, ("document.put", "table.rows_changed"), 42)
    assert codec.encode(VAULT_ID, ("table.rows_changed", "document.put"), 42) == token
    assert EventCursorCodec("cursor-secret").encode(VAULT_ID, ("document.put", "table.rows_changed"), 42) == token
    assert EventCursorCodec("cursor-secret").encode(VAULT_ID, (), 42) != token

    tamper_at = len("ec1.") + 8
    replacement = "A" if token[tamper_at] != "A" else "B"
    tampered = token[:tamper_at] + replacement + token[tamper_at + 1 :]
    with pytest.raises(EventCursorError):
        codec.decode(tampered)


def test_kind_filter_is_exact_sorted_deduplicated_and_rejects_wildcards() -> None:
    assert normalize_kind_filter(["table.rows_changed", "document.put", "table.rows_changed"]) == (
        "document.put",
        "table.rows_changed",
    )
    assert normalize_kind_filter(None) == ()

    with pytest.raises(EventCursorError):
        normalize_kind_filter(["document.*"])


def test_start_position_matrix_obeys_header_priority_and_earliest_exclusion() -> None:
    codec = EventCursorCodec("cursor-secret")
    header = codec.encode(VAULT_ID, (), 12)
    query = codec.encode(VAULT_ID, (), 7)
    bounds = EventBounds(earliest_id=3, latest_id=20)

    assert resolve_start_position(
        codec,
        VAULT_ID,
        (),
        bounds,
        last_event_id=header,
        cursor=query,
        start=None,
    ) == 12
    assert resolve_start_position(
        codec,
        VAULT_ID,
        (),
        bounds,
        last_event_id=header,
        cursor="not-a-cursor",
        start=None,
    ) == 12
    assert resolve_start_position(
        codec,
        VAULT_ID,
        (),
        bounds,
        last_event_id=None,
        cursor=query,
        start=None,
    ) == 7
    assert resolve_start_position(
        codec,
        VAULT_ID,
        (),
        bounds,
        last_event_id=None,
        cursor=None,
        start="earliest",
    ) == 2
    assert resolve_start_position(
        codec,
        VAULT_ID,
        (),
        bounds,
        last_event_id=None,
        cursor=None,
        start=None,
    ) == 20

    with pytest.raises(EventCursorError):
        resolve_start_position(
            codec,
            VAULT_ID,
            (),
            bounds,
            last_event_id=header,
            cursor=None,
            start="earliest",
        )


def test_cursor_scope_and_retention_gap_fail_closed_with_recovery_cursors() -> None:
    codec = EventCursorCodec("cursor-secret")
    # Retention deleted the Vault's events up to 9.
    bounds = EventBounds(earliest_id=10, latest_id=25, deleted_through=9)

    with pytest.raises(EventCursorError):
        resolve_start_position(
            codec,
            UUID("22222222-2222-4222-8222-222222222222"),
            (),
            bounds,
            last_event_id=codec.encode(VAULT_ID, (), 9),
            cursor=None,
            start=None,
        )

    with pytest.raises(EventGapError) as exc_info:
        validate_event_gap(codec, VAULT_ID, (), position=8, bounds=bounds)

    assert codec.decode(exc_info.value.details["earliest_cursor"]) == (VAULT_ID, (), 9)
    assert codec.decode(exc_info.value.details["latest_cursor"]) == (VAULT_ID, (), 25)

    # A cursor immediately before the retained head is a valid way to request
    # that first retained event, and a cursor at the current tail waits.
    validate_event_gap(codec, VAULT_ID, (), position=9, bounds=bounds)
    validate_event_gap(codec, VAULT_ID, (), position=25, bounds=bounds)


def test_a_cursor_far_below_a_vaults_first_event_is_no_gap_until_something_is_deleted() -> None:
    """Event ids are shared by every Vault, so other Vaults' events advance them.

    A cursor taken on an empty Vault is far below its first event without
    having missed anything; only a deletion makes a gap, and the refusal
    resumes at the highest deleted id.
    """
    codec = EventCursorCodec("cursor-secret")

    nothing_deleted = EventBounds(earliest_id=101, latest_id=140)
    validate_event_gap(codec, VAULT_ID, (), position=0, bounds=nothing_deleted)

    swept = EventBounds(earliest_id=121, latest_id=140, deleted_through=120)
    validate_event_gap(codec, VAULT_ID, (), position=120, bounds=swept)
    with pytest.raises(EventGapError) as exc_info:
        validate_event_gap(codec, VAULT_ID, (), position=0, bounds=swept)
    assert codec.decode(exc_info.value.details["earliest_cursor"]) == (VAULT_ID, (), 120)
    assert codec.decode(exc_info.value.details["latest_cursor"]) == (VAULT_ID, (), 140)


@pytest.mark.parametrize(
    ("bounds", "earliest", "latest"),
    [
        # Nothing deleted: just before the first retained event, or the last one.
        (EventBounds(earliest_id=101, latest_id=140), 100, 140),
        # Deleted from the start: the same, the watermark sitting just below.
        (EventBounds(earliest_id=121, latest_id=140, deleted_through=120), 120, 140),
        # A late event outlived a later deleted one: earliest starts after the
        # deletion, where the tail is complete, so it is never refused.
        (EventBounds(earliest_id=103, latest_id=140, deleted_through=105), 105, 140),
        # Everything deleted: both start at the watermark.
        (EventBounds(earliest_id=None, latest_id=None, deleted_through=120), 120, 120),
        # An empty Vault never swept.
        (EventBounds(earliest_id=None, latest_id=None), 0, 0),
    ],
)
def test_start_positions_never_land_below_the_watermark(bounds: EventBounds, earliest: int, latest: int) -> None:
    codec = EventCursorCodec("cursor-secret")

    def resolved(start: str | None) -> int:
        return resolve_start_position(
            codec, VAULT_ID, (), bounds, last_event_id=None, cursor=None, start=start
        )

    assert (resolved("earliest"), resolved(None)) == (earliest, latest)
    for position in (earliest, latest):
        validate_event_gap(codec, VAULT_ID, (), position=position, bounds=bounds)


def test_sse_records_and_heartbeat_keep_control_data_distinct() -> None:
    frame = format_sse("checkpoint", "ec1.cursor", {"version": 1, "cursor": "ec1.cursor"})

    assert frame == ('event: checkpoint\nid: ec1.cursor\ndata: {"version":1,"cursor":"ec1.cursor"}\n\n')
    assert format_heartbeat() == ": heartbeat\n\n"


def test_public_envelopes_require_version_and_reject_internal_fields() -> None:
    from app.models.events import ChangeEventEnvelopeV1, TailCheckpointV1

    with pytest.raises(ValidationError):
        ChangeEventEnvelopeV1.model_validate(
            {
                "cursor": "ec1.cursor",
                "occurred_at": "2026-08-28T08:00:00Z",
                "vault": "vault",
                "kind": "document.put",
                "payload": {},
            }
        )
    checkpoint = TailCheckpointV1(version=1, cursor="ec1.cursor")
    with pytest.raises(ValidationError):
        checkpoint.model_validate({"version": 1, "cursor": "ec1.cursor", "id": 4})


@pytest.mark.asyncio
async def test_stream_emits_checkpoint_for_skipped_event_and_change_envelope(monkeypatch, hub) -> None:
    from app.services import event_tail_service

    rows = [
        {
            "id": 10,
            "occurred_at": datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
            "kind": "document.update",
            "resource_uri": "akb://vault/doc/a.md",
            "actor_id": "alice",
            "payload": {"changed": True},
        },
        {
            "id": 11,
            "occurred_at": datetime(2026, 8, 28, 8, 1, tzinfo=timezone.utc),
            "kind": "table.rows_changed",
            "resource_uri": "akb://vault/table/items",
            "actor_id": "alice",
            "payload": {"operation": "insert"},
        },
    ]
    pool, connection, request = _stream_test_doubles()
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    list_events = AsyncMock(return_value=rows)
    monkeypatch.setattr(event_tail_service, "list_vault_events", list_events)
    access = AsyncMock(return_value={"vault_id": VAULT_ID})
    monkeypatch.setattr(event_tail_service, "check_vault_access", access)

    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=("table.rows_changed",),
        position=9,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(9, kinds=("table.rows_changed",)),
        checked_at=asyncio.get_running_loop().time(),
    )
    # Where the stream starts, before anything is read.
    start = await anext(stream)
    checkpoint = await anext(stream)
    # The batch was read on a pooled connection that went back before the
    # first frame was handed out.
    assert pool.checked_out == 0
    change = await anext(stream)
    assert pool.checked_out == 0
    await stream.aclose()

    codec = EventCursorCodec("cursor-secret")
    assert start == _checkpoint_at(9, kinds=("table.rows_changed",))
    assert checkpoint == _checkpoint_at(10, kinds=("table.rows_changed",))
    assert change.startswith("event: change\nid: ec1.")
    assert codec.decode(change.split("\n", 2)[1][4:])[2] == 11
    checkpoint_body = json.loads(checkpoint.split("data: ", 1)[1])
    change_body = json.loads(change.split("data: ", 1)[1])
    assert checkpoint_body["version"] == 1
    assert checkpoint_body["cursor"] == checkpoint.split("\n", 2)[1][4:]
    assert change_body["version"] == 1
    assert change_body["cursor"] == change.split("\n", 2)[1][4:]
    assert change_body["kind"] == "table.rows_changed"
    assert change_body["payload"] == {"operation": "insert"}
    assert "id" not in change_body
    assert "vault_id" not in change_body
    assert list_events.await_args.args[0] is connection
    assert list_events.await_args.kwargs == {"after_id": 9, "limit": event_tail_service.EVENT_BATCH_SIZE}
    assert hub.subscriptions == []
    assert len(hub.unsubscribed) == 1
    # The stream asked to be woken for its own Vault's events only.
    assert hub.unsubscribed[0].vault == str(VAULT_ID)
    # One access check covered the whole batch, on the connection it was read
    # on; the rows did not each borrow a connection for a check of their own.
    assert access.await_count == 1
    assert access.await_args.kwargs["_conn"] is connection


@pytest.mark.asyncio
async def test_empty_wakeups_do_not_restart_heartbeat_deadline(monkeypatch, hub) -> None:
    from app.services import event_tail_service

    pool, connection, request = _stream_test_doubles()
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    list_events = AsyncMock(return_value=[])
    monkeypatch.setattr(event_tail_service, "list_vault_events", list_events)
    monkeypatch.setattr(
        event_tail_service,
        "check_vault_access",
        AsyncMock(return_value={"vault_id": VAULT_ID}),
    )
    monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", 0.04)

    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=0,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(0),
        checked_at=asyncio.get_running_loop().time(),
    )
    notification_task: asyncio.Task[None] | None = None

    async def keep_waking() -> None:
        while True:
            await asyncio.sleep(0.005)
            hub.notify()

    async def subscribed() -> None:
        while not hub.subscriptions:
            await asyncio.sleep(0)

    try:
        assert await anext(stream) == _checkpoint_at(0)
        next_frame = asyncio.create_task(anext(stream))
        await asyncio.wait_for(subscribed(), timeout=0.1)
        notification_task = asyncio.create_task(keep_waking())
        frame = await asyncio.wait_for(next_frame, timeout=0.2)
    finally:
        if notification_task is not None:
            notification_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await notification_task
        await stream.aclose()

    assert frame == format_heartbeat()
    assert list_events.await_count > 1
    # Each empty re-read borrowed a connection and gave it back.
    assert pool.acquisitions == list_events.await_count
    assert pool.checked_out == 0
    assert hub.subscriptions == []
    assert len(hub.unsubscribed) == 1


@pytest.mark.asyncio
async def test_stream_closes_without_emitting_after_access_is_revoked(monkeypatch, hub) -> None:
    """The access check runs after the read, so it covers the rows just read.

    Only the start checkpoint, sent on the access check that opened the
    stream, comes before it.
    """
    from app.exceptions import ForbiddenError
    from app.services import event_tail_service

    pool, connection, request = _stream_test_doubles()
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        event_tail_service,
        "list_vault_events",
        AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "occurred_at": datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
                    "kind": "document.put",
                    "resource_uri": None,
                    "actor_id": "alice",
                    "payload": {},
                }
            ]
        ),
    )
    access = AsyncMock(side_effect=ForbiddenError("revoked"))
    monkeypatch.setattr(event_tail_service, "check_vault_access", access)

    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=0,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(0),
        checked_at=asyncio.get_running_loop().time(),
    )
    assert await anext(stream) == _checkpoint_at(0)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert hub.subscriptions == []
    assert pool.checked_out == 0
    assert access.await_count == 1
    assert access.await_args.kwargs["_conn"] is connection


@pytest.mark.asyncio
async def test_more_waiting_streams_than_pool_connections_leave_the_pool_usable(monkeypatch, hub) -> None:
    """Every open stream used to keep one pooled connection for its whole life.

    A client that opened a stream per Vault therefore held the entire pool, and
    an ordinary checkout, the readiness probe's among them, never returned.
    """
    from app.services import event_tail_service

    pool, connection, _ = _stream_test_doubles(max_size=2)
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", 30.0)
    committed: list[dict] = []
    reads = 0

    async def list_events(conn, _vault_id, *, after_id: int, limit: int) -> list[dict]:
        nonlocal reads
        assert conn is connection
        reads += 1
        return [row for row in committed if row["id"] > after_id][:limit]

    async def access(*_args, _conn, **_kwargs) -> dict:
        # The real access check runs on the connection the batch was read on.
        assert _conn is connection
        return {"vault_id": VAULT_ID}

    monkeypatch.setattr(event_tail_service, "list_vault_events", list_events)
    monkeypatch.setattr(event_tail_service, "check_vault_access", access)
    codec = EventCursorCodec("cursor-secret")
    streams = []
    for _ in range(5):
        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        streams.append(
            event_tail_service._stream_events(
                request,
                user_id="user",
                vault="vault",
                vault_id=VAULT_ID,
                kinds=(),
                position=0,
                codec=codec,
                start_frame=_checkpoint_at(0),
                checked_at=asyncio.get_running_loop().time(),
            )
        )
    for stream in streams:
        assert await anext(stream) == _checkpoint_at(0)
    first_frames = [asyncio.create_task(anext(stream)) for stream in streams]

    async def all_parked() -> None:
        while reads < len(streams) or pool.checked_out:
            await asyncio.sleep(0.001)

    async def take_the_whole_pool() -> bool:
        async with pool.acquire(), pool.acquire():
            return True

    try:
        await asyncio.wait_for(all_parked(), timeout=1.0)
        assert len(hub.subscriptions) == len(streams)
        assert pool.checked_out == 0
        assert await asyncio.wait_for(take_the_whole_pool(), timeout=1.0)

        committed.append(
            {
                "id": 1,
                "occurred_at": datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
                "kind": "document.put",
                "resource_uri": "akb://vault/doc/a.md",
                "actor_id": "alice",
                "payload": {},
            }
        )
        hub.notify()
        frames = await asyncio.wait_for(asyncio.gather(*first_frames), timeout=1.0)
    finally:
        for task in first_frames:
            task.cancel()
        # A generator can only be closed once its task has let go of it.
        await asyncio.gather(*first_frames, return_exceptions=True)
        for stream in streams:
            await stream.aclose()

    assert all(frame.startswith("event: change\nid: ec1.") for frame in frames)
    assert pool.checked_out == 0
    assert hub.subscriptions == []


def _row(event_id: int) -> dict[str, Any]:
    return {
        "id": event_id,
        "occurred_at": datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
        "kind": "document.put",
        "resource_uri": None,
        "actor_id": "alice",
        "payload": {},
    }


@pytest.mark.asyncio
async def test_a_slow_recheck_during_which_access_was_revoked_sends_no_further_row(monkeypatch, hub) -> None:
    """A check covers rows from when it started, for a second.

    One that took longer has expired by the time it returns, and the access
    it saw may have been revoked while it ran, so it is run again before the
    next row goes out. The re-check used to be trusted however long it took.
    """
    from app.exceptions import ForbiddenError
    from app.services import event_tail_service

    pool, _connection, request = _stream_test_doubles()
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(event_tail_service, "list_vault_events", AsyncMock(return_value=[_row(1), _row(2), _row(3)]))
    window = event_tail_service._RECHECK_DURING_YIELD_SECONDS
    granted = True
    slow_checks = 0
    checks = 0

    async def access(*_args: Any, _conn: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal granted, slow_checks, checks
        checks += 1
        seen = granted
        if slow_checks:
            slow_checks -= 1
            # The queries saw access still granted; it is revoked while the
            # rest of the check, longer than the window, runs.
            granted = False
            await asyncio.sleep(window + 0.2)
        if not seen:
            raise ForbiddenError("revoked")
        return {"vault_id": VAULT_ID}

    monkeypatch.setattr(event_tail_service, "check_vault_access", access)
    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=0,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(0),
        checked_at=asyncio.get_running_loop().time(),
    )
    try:
        assert await anext(stream) == _checkpoint_at(0)
        assert (await anext(stream)).startswith("event: change\n")
        slow_checks = 1
        # A reader slower than the window: the next row needs a new check.
        await asyncio.sleep(window + 0.1)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()

    # The pass's check, the slow re-check, and the one that saw the revocation.
    assert checks == 3
    assert pool.checked_out == 0


@pytest.mark.asyncio
async def test_a_stream_whose_checks_keep_outlasting_the_window_ends(monkeypatch, hub) -> None:
    """Each expired check is repeated, but not forever: the stream ends with no further frame.

    Its client resumes from the last cursor it received.
    """
    from app.services import event_tail_service

    pool, _connection, request = _stream_test_doubles()
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(event_tail_service, "list_vault_events", AsyncMock(return_value=[_row(1), _row(2)]))
    monkeypatch.setattr(event_tail_service, "_RECHECK_DURING_YIELD_SECONDS", 0.2)
    slow = False
    checks = 0

    async def access(*_args: Any, _conn: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal checks
        checks += 1
        if slow:
            await asyncio.sleep(0.3)
        return {"vault_id": VAULT_ID}

    monkeypatch.setattr(event_tail_service, "check_vault_access", access)
    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=0,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(0),
        checked_at=asyncio.get_running_loop().time(),
    )
    try:
        assert await anext(stream) == _checkpoint_at(0)
        assert (await anext(stream)).startswith("event: change\n")
        slow = True
        await asyncio.sleep(0.3)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()

    assert event_tail_service._MAX_STALE_ACCESS_CHECKS == 3
    # The pass's check covered row 1 and aged while the reader was slow, which
    # is not the check's doing; then three re-checks that each took too long.
    assert checks == 1 + event_tail_service._MAX_STALE_ACCESS_CHECKS


def _slow_check_stream(monkeypatch, *, rows: list[dict[str, Any]]) -> tuple[Any, AsyncIterator[str], dict[str, Any]]:
    """A stream over ``rows`` whose access checks take ``state['delay']`` seconds and pass while ``state['granted']``."""
    from app.exceptions import ForbiddenError
    from app.services import event_tail_service

    pool, _connection, request = _stream_test_doubles()
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(event_tail_service, "list_vault_events", AsyncMock(return_value=rows))
    state: dict[str, Any] = {"delay": 0.0, "granted": True, "checks": 0}

    async def access(*_args: Any, _conn: Any, **_kwargs: Any) -> dict[str, Any]:
        state["checks"] += 1
        granted = state["granted"]
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if not granted:
            raise ForbiddenError("revoked")
        return {"vault_id": VAULT_ID}

    monkeypatch.setattr(event_tail_service, "check_vault_access", access)
    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=0,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(0),
        checked_at=asyncio.get_running_loop().time(),
    )
    return event_tail_service, stream, state


def _checkpoint_at(position: int, *, kinds: tuple[str, ...] = ()) -> str:
    cursor = EventCursorCodec("cursor-secret").encode(VAULT_ID, kinds, position)
    return format_sse("checkpoint", cursor, {"version": 1, "cursor": cursor})


@pytest.mark.asyncio
async def test_the_pass_check_counts_toward_the_slow_check_limit(monkeypatch, hub) -> None:
    """A pass whose own check expired before its first row is one slow check of three, not a free one."""
    service, stream, state = _slow_check_stream(monkeypatch, rows=[_row(1), _row(2)])
    monkeypatch.setattr(service, "_RECHECK_DURING_YIELD_SECONDS", 0.2)
    state["delay"] = 0.3
    try:
        frames = [frame async for frame in stream]
    finally:
        await stream.aclose()

    # Only the start checkpoint, sent on the open's check; the client resumes
    # from it.
    assert frames == [_checkpoint_at(0)]
    assert state["checks"] == service._MAX_STALE_ACCESS_CHECKS == 3


@pytest.mark.asyncio
async def test_heartbeats_keep_flowing_while_slow_rechecks_run(monkeypatch, hub) -> None:
    """Checks of 6 s against a 15 s heartbeat, scaled down: three in a row outlast a heartbeat interval."""
    service, stream, state = _slow_check_stream(monkeypatch, rows=[_row(1), _row(2)])
    heartbeat = 0.5
    monkeypatch.setattr(service, "HEARTBEAT_INTERVAL_SECONDS", heartbeat)
    monkeypatch.setattr(service, "_RECHECK_DURING_YIELD_SECONDS", 0.1)
    loop = asyncio.get_running_loop()
    try:
        assert await anext(stream) == _checkpoint_at(0)
        assert (await anext(stream)).startswith("event: change\n")
        state["delay"] = 0.2
        # A reader a little slower than the window: the next row needs a new check.
        await asyncio.sleep(0.15)
        arrivals: list[tuple[float, str]] = []
        asked_at = loop.time()
        async for frame in stream:
            arrivals.append((loop.time(), frame))
        ended_at = loop.time()
    finally:
        await stream.aclose()

    # A heartbeat while the third check ran, and no frame once access could
    # not be confirmed in time.
    assert [frame for _, frame in arrivals] == [format_heartbeat()]
    moments = [asked_at] + [at for at, _ in arrivals] + [ended_at]
    gaps = [later - earlier for earlier, later in zip(moments, moments[1:])]
    # No silence longer than a heartbeat interval, with slack for a loaded loop.
    assert max(gaps) < heartbeat + 0.25


@pytest.mark.asyncio
async def test_freshness_is_judged_after_the_frame_is_built(monkeypatch, hub) -> None:
    """Building a frame takes time too; a check that expires while it is built is renewed before sending."""
    service, stream, state = _slow_check_stream(monkeypatch, rows=[_row(1)])
    window = 0.2
    monkeypatch.setattr(service, "_RECHECK_DURING_YIELD_SECONDS", window)
    build_envelope = service._event_envelope

    def slow_envelope(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # A large payload takes longer than the window to serialize, and
        # access is revoked meanwhile.
        time.sleep(window + 0.1)
        state["granted"] = False
        return build_envelope(*args, **kwargs)

    monkeypatch.setattr(service, "_event_envelope", slow_envelope)
    try:
        frames = [frame async for frame in stream]
    finally:
        await stream.aclose()

    assert frames == [_checkpoint_at(0)]
    # The pass's check, and the one before sending that found the revocation.
    assert state["checks"] == 2


def test_the_start_checkpoint_goes_first_with_nothing_before_it(monkeypatch) -> None:
    """open_tail builds the start checkpoint; the stream sends it before touching anything.

    Nothing is awaited first, so no heartbeat can precede it.
    """
    from app.services import event_tail_service

    def untouchable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("touched before the start checkpoint was sent")

    monkeypatch.setattr(event_tail_service, "get_event_tail_hub", untouchable)
    request = MagicMock()
    request.is_disconnected = untouchable
    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=7,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(7),
        checked_at=0.0,
    )
    # One step of the generator, driven without an event loop: it yields at
    # once, so nothing was awaited.
    with pytest.raises(StopIteration) as first:
        stream.__anext__().send(None)
    assert first.value.value == _checkpoint_at(7)
    with pytest.raises(StopIteration):
        stream.aclose().send(None)


@pytest.mark.asyncio
async def test_an_idle_stream_checks_access_right_after_a_heartbeat_sent_during_a_long_read(monkeypatch, hub) -> None:
    """A heartbeat that goes out while a read runs owes a check, even when the read finds nothing.

    The check used to wait for the next heartbeat, so a stream that lost
    access while it read stayed open, and sent another heartbeat, a heartbeat
    longer than it should.
    """
    from app.exceptions import ForbiddenError
    from app.services import event_tail_service

    pool, _connection, request = _stream_test_doubles()
    heartbeat = 0.4
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", heartbeat)
    loop = asyncio.get_running_loop()
    reads = 0
    granted = True
    check_times: list[float] = []

    async def first_read_slow(_conn: Any, _vault_id: Any, *, after_id: int, limit: int) -> list[dict[str, Any]]:
        nonlocal reads
        reads += 1
        if reads == 1:
            await asyncio.sleep(heartbeat + 0.15)
        return []

    async def access(*_args: Any, _conn: Any, **_kwargs: Any) -> dict[str, Any]:
        check_times.append(loop.time())
        if not granted:
            raise ForbiddenError("revoked")
        return {"vault_id": VAULT_ID}

    monkeypatch.setattr(event_tail_service, "list_vault_events", first_read_slow)
    monkeypatch.setattr(event_tail_service, "check_vault_access", access)
    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=0,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(0),
        checked_at=loop.time(),
    )
    try:
        assert await anext(stream) == _checkpoint_at(0)
        # The first read is still running when its heartbeat falls due.
        assert await anext(stream) == format_heartbeat()
        heartbeat_at = loop.time()
        granted = False
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        closed_at = loop.time()
    finally:
        await stream.aclose()

    # The check ran as soon as the empty read ended, not a heartbeat later, and
    # no further heartbeat went out.
    assert check_times and check_times[0] - heartbeat_at < 0.3
    assert closed_at - heartbeat_at < heartbeat


@pytest.mark.asyncio
async def test_heartbeats_keep_flowing_while_a_slow_pass_runs(monkeypatch, hub) -> None:
    """A batch read or its check can take longer than a heartbeat interval; the heartbeat does not wait."""
    from app.services import event_tail_service

    pool, _connection, request = _stream_test_doubles()
    heartbeat = 0.1
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", heartbeat)
    monkeypatch.setattr(event_tail_service, "check_vault_access", AsyncMock(return_value={"vault_id": VAULT_ID}))

    async def slow_read(_conn: Any, _vault_id: Any, *, after_id: int, limit: int) -> list[dict[str, Any]]:
        # The database answers the first read only after several heartbeat intervals.
        await asyncio.sleep(0.35)
        return [_row(1)] if after_id == 0 else []

    monkeypatch.setattr(event_tail_service, "list_vault_events", slow_read)
    loop = asyncio.get_running_loop()
    stream = event_tail_service._stream_events(
        request,
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=0,
        codec=EventCursorCodec("cursor-secret"),
        start_frame=_checkpoint_at(0),
        checked_at=loop.time(),
    )
    arrivals: list[tuple[float, str]] = []
    try:
        assert await anext(stream) == _checkpoint_at(0)
        asked_at = loop.time()
        async for frame in stream:
            arrivals.append((loop.time(), frame))
            if frame.startswith("event: change\n"):
                break
    finally:
        await stream.aclose()

    frames = [frame for _, frame in arrivals]
    assert frames[:-1] and set(frames[:-1]) == {format_heartbeat()}
    assert frames[-1].startswith("event: change\n")
    moments = [asked_at] + [at for at, _ in arrivals]
    assert max(later - earlier for earlier, later in zip(moments, moments[1:])) < heartbeat + 0.1


@pytest.mark.asyncio
async def test_tail_database_work_holds_no_more_connections_than_its_budget(monkeypatch) -> None:
    from app.services import event_tail_service

    pool, _connection, _ = _stream_test_doubles()
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(event_tail_service, "_db_budget", None)
    monkeypatch.setenv("AKB_EVENT_TAIL_DB_CONCURRENCY", "2")
    finish = asyncio.Event()
    most_checked_out = 0

    async def borrow() -> None:
        nonlocal most_checked_out
        async with event_tail_service._tail_db_connection():
            most_checked_out = max(most_checked_out, pool.checked_out)
            await finish.wait()

    def budget() -> tuple[int, int]:
        snapshot = event_tail_service.stats_snapshot()
        return snapshot["budget_in_use"], snapshot["budget_waiters"]

    async def settled(expected: tuple[int, int]) -> None:
        while budget() != expected:
            await asyncio.sleep(0)

    borrowers = [asyncio.create_task(borrow()) for _ in range(10)]
    try:
        await asyncio.wait_for(settled((2, 8)), timeout=1.0)
        assert pool.checked_out == 2
        # A borrower that gives up while waiting takes nothing with it.
        for waiting in borrowers[-3:]:
            waiting.cancel()
        await asyncio.wait_for(settled((2, 5)), timeout=1.0)
        finish.set()
        await asyncio.gather(*borrowers, return_exceptions=True)
    finally:
        finish.set()
        for task in borrowers:
            task.cancel()
        await asyncio.gather(*borrowers, return_exceptions=True)

    assert most_checked_out == 2
    assert pool.checked_out == 0
    assert budget() == (0, 0)
    assert event_tail_service.stats_snapshot()["budget_size"] == 2
    # Every connection went back with the return deadline.
    assert pool.release_timeouts == [event_tail_service._RETURN_DEADLINE_SECONDS] * 7


def test_the_database_budget_belongs_to_the_event_loop_that_uses_it(monkeypatch) -> None:
    """An asyncio semaphore that has waited on one loop refuses to wait on another.

    Each loop gets a budget of its own, so a process (or a test run) that starts
    a new loop does not inherit one it can no longer wait on.
    """
    from app.services import event_tail_service

    pool, _connection, _ = _stream_test_doubles()
    monkeypatch.setattr(event_tail_service, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(event_tail_service, "_db_budget", None)
    monkeypatch.setenv("AKB_EVENT_TAIL_DB_CONCURRENCY", "1")

    async def contend() -> Any:
        async def borrow() -> None:
            async with event_tail_service._tail_db_connection():
                await asyncio.sleep(0.01)

        # The second borrower waits, which ties the semaphore to this loop.
        await asyncio.gather(borrow(), borrow())
        return event_tail_service._db_budget

    first = asyncio.run(contend())
    second = asyncio.run(contend())

    assert first is not None and second is not None
    assert first is not second
    assert (second.size, second.in_use, second.waiters) == (1, 0, 0)


def _on_a_thread_of_its_own(action: Callable[[], Any], *, timeout: float = 5.0) -> Any:
    """Run ``action`` on a new thread and return its result or raise its error.

    A thread that waits on a lock it already holds never returns. On a daemon
    thread of its own, that deadlock fails the test after ``timeout`` instead
    of hanging the run.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = action()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test's thread
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="event-tail-slot-probe", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        pytest.fail(f"stream slot accounting deadlocked: the thread was still blocked after {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


class _AbandonedBody:
    """Stands in for the body of a stream whose client left before it started.

    It is in a reference cycle, so only the garbage collector frees it, and the
    ``weakref.finalize`` that open_tail registers then gives its slot back from
    inside that collection.
    """

    def __init__(self) -> None:
        self.cycle = self


# How each refusal is reached: the stream already admitted, the newcomer
# refused, and the error class that refuses it. The process cap refuses a
# second account; the per-account quota refuses the same account again.
_REFUSALS = {
    "process-cap": (
        {"user_id": "first-account", "limit": 1, "per_user_limit": 10},
        {"user_id": "second-account", "limit": 1, "per_user_limit": 10},
        "EventTailCapacityError",
    ),
    "account-quota": (
        {"user_id": "one-account", "limit": 10, "per_user_limit": 1},
        {"user_id": "one-account", "limit": 10, "per_user_limit": 1},
        "EventTailUserQuotaError",
    ),
}


@pytest.mark.parametrize("refusal", sorted(_REFUSALS))
def test_a_slot_freed_by_a_collection_the_refusal_starts_does_not_deadlock(monkeypatch, refusal: str) -> None:
    """A refusal used to build its error while it held the slot lock.

    Building an object can start a garbage collection on the same thread. When
    that collection finalizes a stream whose client left, the finalizer gives
    the slot back through the same lock, and the thread waits on itself, with
    the event loop it runs.
    """
    from app.services import event_tail_service

    admitted, refused, error_name = _REFUSALS[refusal]
    refusal_error = getattr(event_tail_service, error_name)
    slots = event_tail_service._StreamSlots()
    abandoned = slots.acquire(**admitted)
    lock_held_while_refusing: list[bool] = []

    class _CollectingRefusal(refusal_error):
        def __init__(self, **kwargs: Any) -> None:
            lock_held_while_refusing.append(slots._lock.locked())
            # The allocation that happens to start a collection.
            gc.collect()
            super().__init__(**kwargs)

    monkeypatch.setattr(event_tail_service, error_name, _CollectingRefusal)
    collecting = gc.isenabled()
    # Only the collection the refusal starts may free the abandoned body.
    gc.disable()
    try:
        body = _AbandonedBody()
        # What open_tail registers for a body whose client left before it started.
        finalizer = weakref.finalize(body, abandoned.abandon)
        del body
        assert finalizer.alive
        assert slots.open == 1

        with pytest.raises(refusal_error):
            _on_a_thread_of_its_own(lambda: slots.acquire(**refused))
    finally:
        if collecting:
            gc.enable()

    assert not finalizer.alive
    assert slots.open == 0
    assert slots.users == 0
    assert lock_held_while_refusing == [False]


def test_a_finalizer_run_between_reading_and_writing_an_account_count_leaves_it_exact() -> None:
    """A trace function runs Python between two lines, and a collection can run with it.

    A debugger or profiler installs one. A finalizer that gave a slot back
    there, after acquire had read the account's count and before it wrote it,
    used to change the count underneath it: the stale write then undid the
    release, the account stayed counted for a stream it no longer had, and it
    was later refused with a 429 below its quota.
    """
    from app.services import event_tail_service

    slots = event_tail_service._StreamSlots()
    limits = {"limit": 10, "per_user_limit": 3}
    abandoned = slots.acquire("account", **limits)
    kept = slots.acquire("account", **limits)
    body = _AbandonedBody()
    # What open_tail registers for a body whose client left before it started.
    finalizer = weakref.finalize(body, abandoned.abandon)
    del body
    acquire_code = event_tail_service._StreamSlots.acquire.__code__
    fired: list[str] = []

    def trace_lines(frame: Any, event: str, _arg: Any) -> Any:
        # The first line after the count was read: run what a collection
        # starting there would run.
        if event == "line" and not fired and "held" in frame.f_locals:
            fired.append("finalizer")
            finalizer()
        return trace_lines

    def trace_calls(frame: Any, _event: str, _arg: Any) -> Any:
        return trace_lines if frame.f_code is acquire_code else None

    previous = sys.gettrace()
    sys.settrace(trace_calls)
    try:
        newcomer = slots.acquire("account", **limits)
    finally:
        sys.settrace(previous)

    assert fired == ["finalizer"]
    assert not finalizer.alive
    # Exactly the kept stream and the newcomer.
    assert (slots.open, slots.users) == (2, 1)
    assert slots._per_user == {"account": 2}
    kept.release()
    newcomer.release()
    assert (slots.open, slots.users) == (0, 0)
    # The whole quota is the account's again: no 429 below it.
    again = [slots.acquire("account", **limits) for _ in range(3)]
    for slot in again:
        slot.release()
    assert (slots.open, slots.users) == (0, 0)


class _CollectsWhileHeld:
    """The slots' own lock, with a collection run on the thread that holds it.

    CPython runs a collection that is due when a call returns, so one can start
    right after ``acquire`` has taken its lock; a later edit could add a call
    anywhere before the lock is let go. ``callback`` runs once, at ``point``.
    """

    def __init__(self, lock: Any, callback: Callable[[], object], *, point: str) -> None:
        self._lock = lock
        self._callback: Callable[[], object] | None = callback
        self._point = point

    def __enter__(self) -> _CollectsWhileHeld:
        self._lock.acquire()
        if self._point == "taken":
            try:
                self._fire()
            except BaseException:
                self._lock.release()
                raise
        return self

    def __exit__(self, *_exc_info: object) -> None:
        try:
            if self._point == "letting-go":
                self._fire()
        finally:
            self._lock.release()

    def _fire(self) -> None:
        callback, self._callback = self._callback, None
        if callback is not None:
            callback()


# A cap of two, reached through the process cap (three accounts) or through
# one account's quota (the same account three times).
_FULL_AT_TWO = {
    "process-cap": (
        ("first-account", "second-account", "third-account"),
        {"limit": 2, "per_user_limit": 10},
        "EventTailCapacityError",
    ),
    "account-quota": (
        ("one-account", "one-account", "one-account"),
        {"limit": 10, "per_user_limit": 2},
        "EventTailUserQuotaError",
    ),
}


@pytest.mark.parametrize("giving_back", ["finalizer", "direct-release"])
@pytest.mark.parametrize("refusal", sorted(_FULL_AT_TWO))
@pytest.mark.parametrize(
    ("point", "admitted", "still_open"),
    [
        # Freed before the count is read: the newcomer takes the freed slot.
        pytest.param("taken", True, 2, id="freed-right-after-the-lock-is-taken"),
        # Freed after the newcomer was refused: one stream is left open.
        pytest.param("letting-go", False, 1, id="freed-just-before-the-lock-is-let-go"),
    ],
)
def test_a_slot_freed_on_the_admitting_thread_while_it_holds_the_lock_keeps_the_count(
    monkeypatch,
    point: str,
    admitted: bool,
    still_open: int,
    refusal: str,
    giving_back: str,
) -> None:
    from app.services import event_tail_service

    (abandoned_user, kept_user, newcomer_user), limits, error_name = _FULL_AT_TWO[refusal]
    slots = event_tail_service._StreamSlots()
    abandoned = slots.acquire(abandoned_user, **limits)
    kept = slots.acquire(kept_user, **limits)
    # A finalizer only queues the slot; ordinary code may still release
    # directly, and the lock lets it in.
    callback = abandoned.abandon if giving_back == "finalizer" else abandoned.release
    monkeypatch.setattr(slots, "_lock", _CollectsWhileHeld(slots._lock, callback, point=point))

    newcomer = None
    if admitted:
        newcomer = _on_a_thread_of_its_own(lambda: slots.acquire(newcomer_user, **limits))
    else:
        with pytest.raises(getattr(event_tail_service, error_name)):
            _on_a_thread_of_its_own(lambda: slots.acquire(newcomer_user, **limits))

    assert slots.open == still_open
    # A slot given back twice is counted once.
    abandoned.release()
    assert slots.open == still_open
    kept.release()
    if newcomer is not None:
        newcomer.release()
    assert slots.open == 0
    # An account leaves the per-account count with its last stream.
    assert slots.users == 0
