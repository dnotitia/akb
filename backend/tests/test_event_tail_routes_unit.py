"""Unit coverage for the authenticated Event Tail route boundary."""

from __future__ import annotations

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
