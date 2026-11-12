"""Unit coverage for the authenticated Event Tail route boundary."""

from __future__ import annotations

import asyncio
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
            return {"earliest_id": 10, "latest_id": 20}

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
        return {"earliest_id": None, "latest_id": None}


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

    # The client went away: the stream ends and gives its slot back.
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
    reading = asyncio.create_task(anext(response.body_iterator))

    async def parked() -> None:
        while not tail.hub.subscriptions:
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
            return {"earliest_id": 10, "latest_id": 20}

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
    reading = asyncio.create_task(anext(response.body_iterator))

    async def parked() -> None:
        while not tail.hub.subscriptions:
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
