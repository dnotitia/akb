"""Unit contracts for the PostgreSQL-backed event tail."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import gc
import json
import threading
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

    def subscribe(self) -> EventTailSubscription:
        subscription = EventTailSubscription()
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

    def acquire(self) -> "_Acquire":
        return _Acquire(self)


class _Acquire:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

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
    bounds = EventBounds(earliest_id=10, latest_id=25)

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
    )
    checkpoint = await anext(stream)
    # The batch was read on a pooled connection that went back before the
    # first frame was handed out.
    assert pool.checked_out == 0
    change = await anext(stream)
    assert pool.checked_out == 0
    await stream.aclose()

    assert checkpoint.startswith("event: checkpoint\nid: ec1.")
    assert change.startswith("event: change\nid: ec1.")
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
    """The access check runs after the read, so it covers the rows just read."""
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
    )
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
            )
        )
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
        finalizer = weakref.finalize(body, abandoned.release)
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
) -> None:
    from app.services import event_tail_service

    (abandoned_user, kept_user, newcomer_user), limits, error_name = _FULL_AT_TWO[refusal]
    slots = event_tail_service._StreamSlots()
    abandoned = slots.acquire(abandoned_user, **limits)
    kept = slots.acquire(kept_user, **limits)
    monkeypatch.setattr(slots, "_lock", _CollectsWhileHeld(slots._lock, abandoned.release, point=point))

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
