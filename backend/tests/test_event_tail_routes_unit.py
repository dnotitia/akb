"""Unit coverage for the authenticated Event Tail route boundary."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import gc
import json
import types
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from starlette.responses import StreamingResponse


class _User:
    user_id = "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_route_leaves_the_access_check_to_open_tail(monkeypatch) -> None:
    """The route borrows no connection of its own: open_tail checks access within its budget."""
    from app.api.routes import events

    opened = AsyncMock(return_value=StreamingResponse(iter(()), media_type="text/event-stream"))
    monkeypatch.setattr(events.event_tail_service, "open_tail", opened)

    request = MagicMock()
    result = await events.events_tail(
        "vault",
        request,
        cursor=None,
        start=None,
        kind=None,
        last_event_id=None,
        user=_User(),  # type: ignore[arg-type]
    )

    assert result is opened.return_value
    assert not hasattr(events, "check_vault_access")
    opened.assert_awaited_once_with(
        request=request,
        user_id=_User.user_id,
        vault="vault",
        last_event_id=None,
        cursor=None,
        start=None,
        kinds=None,
    )


@pytest.mark.asyncio
async def test_invalid_cursor_is_the_standard_400_error(monkeypatch) -> None:
    from app.api.routes import events
    from app.services.event_tail_service import EventCursorError

    monkeypatch.setattr(
        events.event_tail_service,
        "open_tail",
        AsyncMock(side_effect=EventCursorError("invalid")),
    )

    with pytest.raises(events.AKBError) as exc_info:
        await events.events_tail(
            "vault",
            MagicMock(),
            cursor="bad",
            start=None,
            kind=None,
            last_event_id=None,
            user=_User(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "invalid_event_cursor"


@pytest.mark.asyncio
async def test_open_tail_preflights_retention_gap_before_returning_stream(monkeypatch) -> None:
    from app.config import settings
    from app.services import event_tail_service

    class _Connection:
        async def fetchrow(self, *_args):
            # Retention deleted the Vault's events up to 9.
            return {"earliest_id": 10, "latest_id": 20, "deleted_through": 9}

    class _Acquire:
        def __await__(self):
            return self.__aenter__().__await__()

        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

        async def release(self, _connection, *, timeout=None):
            return None

    monkeypatch.setattr(settings, "system_hmac_secret", "cursor-secret")

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
    monkeypatch.setattr(
        event_tail_service,
        "check_vault_access",
        AsyncMock(return_value={"vault_id": UUID("11111111-1111-4111-8111-111111111111")}),
    )
    codec = event_tail_service.EventCursorCodec("cursor-secret")

    with pytest.raises(event_tail_service.EventGapError) as exc_info:
        await event_tail_service.open_tail(
            MagicMock(),
            user_id="user",
            vault="vault",
            last_event_id=None,
            cursor=codec.encode(UUID("11111111-1111-4111-8111-111111111111"), (), 8),
            start=None,
            kinds=None,
        )

    assert exc_info.value.status_code == 410
    assert exc_info.value.code == "event_gap"
    assert set(exc_info.value.details) == {"earliest_cursor", "latest_cursor"}


class _BoundsPool:
    """Answers the open_tail bounds preflight for an empty Vault.

    It is its own connection, lent like an asyncpg checkout: awaited, or used
    as a context manager.
    """

    def acquire(self):
        return self

    def __await__(self):
        return self.__aenter__().__await__()

    async def release(self, _connection, *, timeout=None):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def fetchrow(self, *_args):
        return {"earliest_id": None, "latest_id": None, "deleted_through": None}


class _Hub:
    def __init__(self) -> None:
        self.subscriptions: list = []

    def subscribe(self, vault_id):
        from app.services.event_tail_hub import EventTailSubscription

        subscription = EventTailSubscription(str(vault_id))
        self.subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription) -> None:
        self.subscriptions.remove(subscription)


@pytest.fixture
def tail(monkeypatch):
    """open_tail against doubles, with this test's own slot count."""
    from app.config import settings
    from app.services import event_tail_service

    async def get_pool():
        return _BoundsPool()

    hub = _Hub()
    monkeypatch.setattr(settings, "system_hmac_secret", "cursor-secret")
    monkeypatch.setattr(event_tail_service, "_stream_slots", event_tail_service._StreamSlots())
    monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
    monkeypatch.setattr(event_tail_service, "get_event_tail_hub", lambda: hub)
    monkeypatch.setattr(
        event_tail_service,
        "check_vault_access",
        AsyncMock(return_value={"vault_id": UUID("11111111-1111-4111-8111-111111111111")}),
    )
    # An empty Vault: every batch read finds nothing and the stream waits.
    monkeypatch.setattr(event_tail_service, "list_vault_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(event_tail_service, "HEARTBEAT_INTERVAL_SECONDS", 30.0)

    async def open_stream(*, disconnected: bool = False, user_id: str = "user") -> StreamingResponse:
        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=disconnected)
        return await event_tail_service.open_tail(
            request,
            user_id=user_id,
            vault="vault",
            last_event_id=None,
            cursor=None,
            start=None,
            kinds=None,
        )

    return types.SimpleNamespace(service=event_tail_service, hub=hub, open=open_stream)


_VAULT_ID = UUID("11111111-1111-4111-8111-111111111111")


@pytest.mark.asyncio
async def test_open_tail_checks_access_before_the_bounds_read_on_one_budgeted_connection(tail, monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    pools: list[_BoundsPool] = []

    class _RecordingPool(_BoundsPool):
        async def fetchrow(self, *args):
            calls.append(("bounds", self))
            return await super().fetchrow(*args)

    async def get_pool():
        pool = _RecordingPool()
        pools.append(pool)
        return pool

    async def access(_user_id, _vault, *, required_role, _conn):
        assert required_role == "reader"
        calls.append(("access", _conn))
        return {"vault_id": _VAULT_ID}

    monkeypatch.setattr(tail.service, "get_pool", get_pool)
    monkeypatch.setattr(tail.service, "check_vault_access", access)

    response = await tail.open()

    # Access first, then the Vault's range, both on the one connection the
    # budget lent; nothing else was borrowed.
    assert [name for name, _ in calls] == ["access", "bounds"]
    assert calls[0][1] is calls[1][1] is pools[0]
    assert len(pools) == 1
    del response


@pytest.mark.asyncio
@pytest.mark.parametrize("refused_by", ["process-cap", "account-quota"])
@pytest.mark.parametrize(("denial", "status"), [("forbidden", 403), ("unknown-vault", 404)])
async def test_a_caller_without_access_hears_so_before_any_capacity_refusal(
    tail,
    monkeypatch,
    refused_by: str,
    denial: str,
    status: int,
) -> None:
    """The route always answered 403 or 404 before 429 or 503, and still does.

    A refused request is checked too, within the budget, so a client that has
    no access is told that, which it does not retry, rather than a 503 it would.
    """
    from app.exceptions import AKBError, ForbiddenError, NotFoundError

    if refused_by == "process-cap":
        monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS", "1")
        kept = await tail.open(user_id="other-account")
    else:
        monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS_PER_USER", "1")
        kept = await tail.open(user_id="account")
    access = tail.service.check_vault_access
    access.side_effect = ForbiddenError("no access") if denial == "forbidden" else NotFoundError("Vault", "vault")

    with pytest.raises(AKBError) as exc_info:
        await tail.open(user_id="account")

    assert exc_info.value.status_code == status
    # The refused request was checked on a budgeted connection and took no slot.
    assert access.await_args.kwargs["_conn"] is not None
    assert tail.service.open_stream_count() == 1
    del kept


@pytest.mark.asyncio
async def test_an_open_whose_access_checks_stay_slow_is_a_retryable_503(tail, monkeypatch) -> None:
    """A stream that could not send its start checkpoint used to answer 200 with an empty body.

    Its client, holding no cursor, reconnected without one and started after
    the newest event, so an event committed meanwhile was skipped. Now open_tail
    checks again; checks that stay slow refuse the open with a 503 the client
    retries.
    """
    window = 0.05
    monkeypatch.setattr(tail.service, "_RECHECK_DURING_YIELD_SECONDS", window)
    checks = 0

    async def slow_access(*_args, _conn, **_kwargs):
        nonlocal checks
        checks += 1
        await asyncio.sleep(window * 2)
        return {"vault_id": _VAULT_ID}

    monkeypatch.setattr(tail.service, "check_vault_access", slow_access)

    with pytest.raises(tail.service.EventTailUnavailableError) as exc_info:
        await tail.open()

    refusal = exc_info.value
    assert refusal.status_code == 503
    assert refusal.code == "event_tail_unavailable"
    assert refusal.retry_after_secs == 1
    # The open's check and two more: three slow checks, then the refusal.
    assert checks == tail.service._MAX_STALE_ACCESS_CHECKS == 3
    assert tail.service.open_stream_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("renewal", "status"), [("revoked", 403), ("vault-replaced", 503)])
async def test_an_open_whose_renewed_check_fails_starts_no_stream(tail, monkeypatch, renewal: str, status: int) -> None:
    """The open's check expired during the preflight, and the renewed check fails.

    Access gone is the 403 it has always been. A Vault deleted and created
    again under the same name meanwhile is a 503: the retry opens on the new
    Vault. Either way nothing is sent and no slot is held.
    """
    from app.exceptions import AKBError, ForbiddenError

    window = 0.05
    monkeypatch.setattr(tail.service, "_RECHECK_DURING_YIELD_SECONDS", window)
    checks = 0

    async def renewed_access(*_args, _conn, **_kwargs):
        nonlocal checks
        checks += 1
        if checks == 1:
            # The open's own check outlasts the window it covers.
            await asyncio.sleep(window * 2)
            return {"vault_id": _VAULT_ID}
        if renewal == "revoked":
            raise ForbiddenError("revoked")
        return {"vault_id": UUID("22222222-2222-4222-8222-222222222222")}

    monkeypatch.setattr(tail.service, "check_vault_access", renewed_access)

    with pytest.raises(AKBError) as exc_info:
        await tail.open()

    assert exc_info.value.status_code == status
    assert checks == 2
    assert tail.service.open_stream_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("later_check", "checks"), [("granted", 2), ("revoked", 2), ("unreachable", 4)])
async def test_the_checkpoint_goes_first_however_long_the_response_start_takes(
    tail, monkeypatch, later_check: str, checks: int
) -> None:
    """The start checkpoint is part of the response to the authorized open.

    It carries no event, only the position the open computed under its
    check, so it goes out first however long the server takes to send the
    response's start. Access is checked before the first event: a reader
    revoked meanwhile gets the checkpoint and no event, and when a database
    fault keeps three checks in a row from being made the body ends before
    the event. The stream used to check again before the checkpoint after a
    slow start, and a check that failed or stayed slow ended the body with
    no frame, leaving a client that brought no cursor without one.
    """
    from app.exceptions import ForbiddenError

    window = 0.05
    monkeypatch.setattr(tail.service, "_RECHECK_DURING_YIELD_SECONDS", window)
    calls = 0
    reads = 0

    async def access(*_args, _conn, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            if later_check == "revoked":
                raise ForbiddenError("revoked")
            if later_check == "unreachable":
                raise ConnectionResetError("connection reset by peer")
        return {"vault_id": _VAULT_ID}

    async def one_event(_conn, _vault_id, *, after_id, limit):
        nonlocal reads
        reads += 1
        if reads > 1:
            return []
        return [
            {
                "id": after_id + 1,
                "occurred_at": datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
                "kind": "document.put",
                "resource_uri": None,
                "actor_id": "alice",
                "payload": {},
            }
        ]

    monkeypatch.setattr(tail.service, "check_vault_access", access)
    monkeypatch.setattr(tail.service, "list_vault_events", one_event)
    response = await tail.open()
    bodies: list[bytes] = []
    event_sent = asyncio.Event()

    async def receive():
        raise AssertionError("an ASGI 2.4 server reports a disconnect as a failed send")

    async def send(message):
        if message["type"] == "http.response.start":
            # The response's start outlasts the window the open's check covers.
            await asyncio.sleep(window * 3)
        elif message["type"] == "http.response.body":
            bodies.append(message["body"])
            if message["body"].startswith(b"event: change\n"):
                event_sent.set()

    responding = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    event = asyncio.create_task(event_sent.wait())
    try:
        # Until the body ends or an event goes out.
        await asyncio.wait({responding, event}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (responding, event):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert bodies and bodies[0].startswith(b"event: checkpoint\n"), f"sent {bodies!r}"
    if later_check == "granted":
        assert bodies[1].startswith(b"event: change\n")
    else:
        # No event reached the reader: the body ended after the checkpoint.
        assert bodies[1:] == [b""], f"sent {bodies!r}"
    assert calls == checks
    assert tail.service.open_stream_count() == 0


@pytest.mark.asyncio
async def test_the_start_checkpoint_comes_before_any_heartbeat_when_the_budget_is_busy(tail, monkeypatch) -> None:
    """The open's check expired while the preflight ran, and the database budget was busy.

    The new check used to run after the response had started, so heartbeats
    went out while it waited for the budget, ahead of the start checkpoint.
    open_tail now makes it before responding.
    """
    window = 0.1
    monkeypatch.setattr(tail.service, "_RECHECK_DURING_YIELD_SECONDS", window)
    monkeypatch.setattr(tail.service, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setenv("AKB_EVENT_TAIL_DB_CONCURRENCY", "1")
    monkeypatch.setattr(tail.service, "_db_budget", None)
    release = asyncio.Event()
    holding = asyncio.Event()
    checks = 0

    class _SlowBoundsPool(_BoundsPool):
        async def fetchrow(self, *args):
            # The preflight outlasts the window the open's check covers.
            await asyncio.sleep(window * 2)
            return await super().fetchrow(*args)

    async def get_pool():
        return _SlowBoundsPool()

    async def counting_access(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        return {"vault_id": _VAULT_ID}

    async def busy_borrower() -> None:
        async with tail.service._tail_db_connection():
            holding.set()
            await release.wait()

    monkeypatch.setattr(tail.service, "get_pool", get_pool)
    monkeypatch.setattr(tail.service, "check_vault_access", counting_access)
    opening = asyncio.create_task(tail.open())
    # Once the preflight is under way, another borrower queues for the slot.
    borrower = asyncio.create_task(busy_borrower())
    await asyncio.wait_for(holding.wait(), timeout=2)
    # It holds the budget for several heartbeat intervals.
    await asyncio.sleep(0.25)
    assert not opening.done()
    release.set()
    response = await asyncio.wait_for(opening, timeout=2)
    await borrower
    try:
        first = await anext(response.body_iterator)
    finally:
        await response.body_iterator.aclose()

    assert first.startswith("event: checkpoint\n")
    assert checks == 2


@pytest.mark.asyncio
async def test_open_tail_refuses_a_stream_past_the_cap_and_admits_again_once_one_ends(tail, monkeypatch) -> None:
    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS", "2")

    ending = await tail.open(disconnected=True)
    kept = await tail.open()
    assert tail.service.open_stream_count() == 2

    with pytest.raises(tail.service.EventTailCapacityError) as exc_info:
        await tail.open()
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "event_tail_capacity"
    assert exc_info.value.retry_after_secs == 5
    assert tail.service.open_stream_count() == 2

    # The client went away: after its start checkpoint the stream ends and
    # gives its slot back.
    assert (await anext(ending.body_iterator)).startswith("event: checkpoint\n")
    with pytest.raises(StopAsyncIteration):
        await anext(ending.body_iterator)
    assert tail.service.open_stream_count() == 1

    replacement = await tail.open()
    assert tail.service.open_stream_count() == 2
    del kept, replacement


@pytest.mark.asyncio
async def test_a_stream_dropped_before_its_body_starts_gives_its_slot_back(tail) -> None:
    response = await tail.open()
    assert tail.service.open_stream_count() == 1

    # The client left before the response began: the generator never ran, so
    # its own `finally` never will either.
    del response
    gc.collect()

    assert tail.service.open_stream_count() == 0
    assert tail.service._stream_slots.users == 0


@pytest.mark.asyncio
async def test_a_cancelled_stream_gives_its_slot_and_subscription_back(tail) -> None:
    response = await tail.open()
    # Every stream starts with a checkpoint at its start position.
    assert (await anext(response.body_iterator)).startswith("event: checkpoint\n")
    reading = asyncio.create_task(anext(response.body_iterator))

    async def parked() -> None:
        # Subscribed, past its first read, and waiting for a wake-up.
        while not tail.hub.subscriptions or not tail.service.list_vault_events.await_count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(parked(), timeout=1.0)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading

    assert tail.service.open_stream_count() == 0
    assert tail.service._stream_slots.users == 0
    assert tail.hub.subscriptions == []


@pytest.mark.asyncio
async def test_a_failed_preflight_gives_its_slot_back(tail, monkeypatch) -> None:
    async def gap_pool():
        pool = _BoundsPool()

        async def fetchrow(*_args):
            return {"earliest_id": 10, "latest_id": 20, "deleted_through": 9}

        pool.fetchrow = fetchrow  # type: ignore[method-assign]
        return pool

    monkeypatch.setattr(tail.service, "get_pool", gap_pool)
    codec = tail.service.EventCursorCodec("cursor-secret")

    with pytest.raises(tail.service.EventGapError):
        await tail.service.open_tail(
            MagicMock(),
            user_id="user",
            vault="vault",
            last_event_id=None,
            cursor=codec.encode(UUID("11111111-1111-4111-8111-111111111111"), (), 8),
            start=None,
            kinds=None,
        )
    with pytest.raises(tail.service.EventCursorError):
        await tail.service.open_tail(
            MagicMock(),
            user_id="user",
            vault="vault",
            last_event_id=None,
            cursor=None,
            start=None,
            kinds=["document.*"],
        )

    assert tail.service.open_stream_count() == 0
    assert tail.service._stream_slots.users == 0


@pytest.mark.asyncio
async def test_an_account_past_its_quota_gets_429_while_other_accounts_still_open_streams(tail, monkeypatch) -> None:
    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS", "10")
    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS_PER_USER", "2")

    kept = [await tail.open(user_id="busy-account") for _ in range(2)]
    with pytest.raises(tail.service.EventTailUserQuotaError) as exc_info:
        await tail.open(user_id="busy-account")
    refusal = exc_info.value
    assert refusal.status_code == 429
    assert refusal.code == "event_tail_user_quota"
    assert refusal.retry_after_secs == 5
    assert refusal.hint == "Close event streams this account no longer reads."
    # The refusal took nothing, and another account is not held to this one's quota.
    assert tail.service.open_stream_count() == 2
    kept.append(await tail.open(user_id="quiet-account"))
    assert tail.service.open_stream_count() == 3
    assert tail.service._stream_slots.users == 2
    del kept


@pytest.mark.asyncio
async def test_the_process_cap_still_refuses_with_503_below_every_account_quota(tail, monkeypatch) -> None:
    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS", "2")
    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS_PER_USER", "5")

    kept = [await tail.open(user_id="first-account"), await tail.open(user_id="second-account")]
    with pytest.raises(tail.service.EventTailCapacityError) as exc_info:
        await tail.open(user_id="third-account")
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "event_tail_capacity"
    assert tail.service.open_stream_count() == 2
    del kept


@pytest.mark.asyncio
async def test_an_account_at_its_quota_on_a_full_server_is_told_to_close_streams(tail, monkeypatch) -> None:
    """Retrying cannot help an account at its quota, so it gets the 429 even when the server is full."""
    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS", "1")
    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS_PER_USER", "1")

    kept = await tail.open(user_id="busy-account")
    with pytest.raises(tail.service.EventTailUserQuotaError):
        await tail.open(user_id="busy-account")
    with pytest.raises(tail.service.EventTailCapacityError):
        await tail.open(user_id="other-account")
    del kept


@pytest.mark.asyncio
async def test_an_account_gets_its_quota_back_however_its_streams_end(tail, monkeypatch) -> None:
    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS_PER_USER", "1")
    slots = tail.service._stream_slots

    # The client left before the body started: the finalizer gives the slot back.
    response = await tail.open(user_id="account")
    assert (slots.open, slots.users) == (1, 1)
    del response
    gc.collect()
    assert (slots.open, slots.users) == (0, 0)

    # The stream was cancelled while it waited.
    response = await tail.open(user_id="account")
    assert (await anext(response.body_iterator)).startswith("event: checkpoint\n")
    reading = asyncio.create_task(anext(response.body_iterator))

    async def parked() -> None:
        # Subscribed, past its first read, and waiting for a wake-up.
        while not tail.hub.subscriptions or not tail.service.list_vault_events.await_count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(parked(), timeout=1.0)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    assert (slots.open, slots.users) == (0, 0)

    # The preflight refused the cursor.
    with pytest.raises(tail.service.EventCursorError):
        await tail.service.open_tail(
            MagicMock(),
            user_id="account",
            vault="vault",
            last_event_id=None,
            cursor="not-a-cursor",
            start=None,
            kinds=None,
        )
    assert (slots.open, slots.users) == (0, 0)

    # Its whole quota is there again.
    kept = await tail.open(user_id="account")
    assert (slots.open, slots.users) == (1, 1)
    del kept


@pytest.mark.asyncio
async def test_quota_refusal_is_a_final_429_on_the_rest_surface(monkeypatch, tmp_path) -> None:
    from app.config import settings

    # Importing the app builds services that create the storage root.
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import akb_error_handler
    from app.services.event_tail_service import EventTailUserQuotaError

    response = await akb_error_handler(None, EventTailUserQuotaError())

    # A 4xx: the SDK's change channel stops on it instead of retrying.
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    body = json.loads(response.body)
    assert body["code"] == "event_tail_user_quota"
    assert body["hint"] == "Close event streams this account no longer reads."


@pytest.mark.asyncio
async def test_capacity_refusal_is_a_retryable_503_on_the_rest_surface(monkeypatch, tmp_path) -> None:
    from app.config import settings

    # Importing the app builds services that create the storage root.
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import akb_error_handler
    from app.services.event_tail_service import EventTailCapacityError

    response = await akb_error_handler(None, EventTailCapacityError())

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    body = json.loads(response.body)
    assert body["code"] == "event_tail_capacity"
    assert body["message"] == "Event tail capacity is exhausted on this server"


@pytest.mark.asyncio
async def test_an_unconfirmed_start_is_a_retryable_503_on_the_rest_surface(monkeypatch, tmp_path) -> None:
    from app.config import settings

    # Importing the app builds services that create the storage root.
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import akb_error_handler
    from app.services.event_tail_service import EventTailUnavailableError

    response = await akb_error_handler(None, EventTailUnavailableError())

    # The Retry-After header the OpenAPI document promises for this 503.
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    body = json.loads(response.body)
    assert body["code"] == "event_tail_unavailable"
    assert body["hint"] == "Retry after the Retry-After delay."


def test_stream_cap_comes_from_the_environment_then_app_yaml(monkeypatch) -> None:
    from app.config import settings
    from app.services import event_tail_service

    monkeypatch.setattr(settings, "event_tail_max_streams", 7)
    monkeypatch.delenv("AKB_EVENT_TAIL_MAX_STREAMS", raising=False)
    assert event_tail_service.configured_max_streams() == 7

    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS", " 12 ")
    assert event_tail_service.configured_max_streams() == 12

    monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS", "")
    assert event_tail_service.configured_max_streams() == 7

    for malformed in ("many", "0", "-3", "1.5"):
        monkeypatch.setenv("AKB_EVENT_TAIL_MAX_STREAMS", malformed)
        with pytest.raises(RuntimeError, match="AKB_EVENT_TAIL_MAX_STREAMS"):
            event_tail_service.configured_max_streams()


def test_app_yaml_stream_cap_defaults_to_256_and_must_be_positive() -> None:
    from pydantic import ValidationError

    from app.config import Settings

    assert Settings(document_revision_backend="bare_git").event_tail_max_streams == 256
    with pytest.raises(ValidationError):
        Settings(document_revision_backend="bare_git", event_tail_max_streams=0)


@pytest.mark.parametrize(
    ("variable", "key", "reader"),
    [
        (
            "AKB_EVENT_TAIL_MAX_STREAMS_PER_USER",
            "event_tail_max_streams_per_user",
            "configured_max_streams_per_user",
        ),
        ("AKB_EVENT_TAIL_DB_CONCURRENCY", "event_tail_db_concurrency", "configured_db_concurrency"),
    ],
)
def test_account_quota_and_database_budget_come_from_the_environment_then_app_yaml(
    monkeypatch,
    variable: str,
    key: str,
    reader: str,
) -> None:
    from app.config import settings
    from app.services import event_tail_service

    configured = getattr(event_tail_service, reader)
    monkeypatch.setattr(settings, key, 7)
    monkeypatch.delenv(variable, raising=False)
    assert configured() == 7

    monkeypatch.setenv(variable, " 12 ")
    assert configured() == 12

    monkeypatch.setenv(variable, "")
    assert configured() == 7

    for malformed in ("many", "0", "-3", "1.5"):
        monkeypatch.setenv(variable, malformed)
        with pytest.raises(RuntimeError, match=variable):
            configured()


def test_app_yaml_account_quota_defaults_to_48_and_database_budget_to_6() -> None:
    from pydantic import ValidationError

    from app.config import Settings

    defaults = Settings(document_revision_backend="bare_git")
    assert defaults.event_tail_max_streams_per_user == 48
    assert defaults.event_tail_db_concurrency == 6
    for key in ("event_tail_max_streams_per_user", "event_tail_db_concurrency"):
        with pytest.raises(ValidationError):
            Settings(document_revision_backend="bare_git", **{key: 0})
