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
async def test_route_authorizes_before_opening_the_event_stream(monkeypatch) -> None:
    from app.api.routes import events

    access = AsyncMock(return_value={"vault_id": UUID("11111111-1111-4111-8111-111111111111")})
    opened = AsyncMock(return_value=StreamingResponse(iter(()), media_type="text/event-stream"))
    monkeypatch.setattr(events, "check_vault_access", access)
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
    access.assert_awaited_once_with(_User.user_id, "vault", required_role="reader")
    opened.assert_awaited_once_with(
        request=request,
        user_id=_User.user_id,
        vault="vault",
        vault_id=UUID("11111111-1111-4111-8111-111111111111"),
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
        events,
        "check_vault_access",
        AsyncMock(return_value={"vault_id": UUID("11111111-1111-4111-8111-111111111111")}),
    )
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
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    monkeypatch.setattr(settings, "system_hmac_secret", "cursor-secret")

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
    codec = event_tail_service.EventCursorCodec("cursor-secret")

    with pytest.raises(event_tail_service.EventGapError) as exc_info:
        await event_tail_service.open_tail(
            MagicMock(),
            user_id="user",
            vault="vault",
            vault_id=UUID("11111111-1111-4111-8111-111111111111"),
            last_event_id=None,
            cursor=codec.encode(UUID("11111111-1111-4111-8111-111111111111"), (), 8),
            start=None,
            kinds=None,
        )

    assert exc_info.value.status_code == 410
    assert exc_info.value.code == "event_gap"
    assert set(exc_info.value.details) == {"earliest_cursor", "latest_cursor"}


class _BoundsPool:
    """Answers the open_tail bounds preflight for an empty Vault."""

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def fetchrow(self, *_args):
        return {"earliest_id": None, "latest_id": None}


class _Hub:
    def __init__(self) -> None:
        self.subscriptions: list = []

    def subscribe(self):
        from app.services.event_tail_hub import EventTailSubscription

        subscription = EventTailSubscription()
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

    async def open_stream(*, disconnected: bool = False) -> StreamingResponse:
        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=disconnected)
        return await event_tail_service.open_tail(
            request,
            user_id="user",
            vault="vault",
            vault_id=UUID("11111111-1111-4111-8111-111111111111"),
            last_event_id=None,
            cursor=None,
            start=None,
            kinds=None,
        )

    return types.SimpleNamespace(service=event_tail_service, hub=hub, open=open_stream)


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
            vault_id=UUID("11111111-1111-4111-8111-111111111111"),
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
            vault_id=UUID("11111111-1111-4111-8111-111111111111"),
            last_event_id=None,
            cursor=None,
            start=None,
            kinds=["document.*"],
        )

    assert tail.service.open_stream_count() == 0


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
