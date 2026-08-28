"""Unit contracts for the PostgreSQL-backed event tail."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

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

    assert (
        resolve_start_position(
            codec,
            VAULT_ID,
            (),
            bounds,
            last_event_id=header,
            cursor=query,
            start=None,
        ).position
        == 12
    )
    assert (
        resolve_start_position(
            codec,
            VAULT_ID,
            (),
            bounds,
            last_event_id=header,
            cursor="not-a-cursor",
            start=None,
        ).position
        == 12
    )
    assert (
        resolve_start_position(
            codec,
            VAULT_ID,
            (),
            bounds,
            last_event_id=None,
            cursor=query,
            start=None,
        ).position
        == 7
    )
    assert (
        resolve_start_position(
            codec,
            VAULT_ID,
            (),
            bounds,
            last_event_id=None,
            cursor=None,
            start="earliest",
        ).position
        == 2
    )
    assert (
        resolve_start_position(
            codec,
            VAULT_ID,
            (),
            bounds,
            last_event_id=None,
            cursor=None,
            start=None,
        ).position
        == 20
    )

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
async def test_stream_emits_checkpoint_for_skipped_event_and_change_envelope(monkeypatch) -> None:
    from app.services import event_tail_service

    class _Connection:
        def __init__(self) -> None:
            self.listeners: list[tuple[str, object]] = []

        async def add_listener(self, channel: str, callback: object) -> None:
            self.listeners.append((channel, callback))

        async def remove_listener(self, channel: str, callback: object) -> None:
            self.listeners.remove((channel, callback))

    class _Acquire:
        def __init__(self, connection: _Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> _Connection:
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Pool:
        def __init__(self, connection: _Connection) -> None:
            self.connection = connection

        def acquire(self) -> _Acquire:
            return _Acquire(self.connection)

    class _Request:
        async def is_disconnected(self) -> bool:
            return False

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
    connection = _Connection()

    async def get_pool() -> _Pool:
        return _Pool(connection)

    monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
    monkeypatch.setattr(event_tail_service, "list_vault_events", AsyncMock(return_value=rows))
    access = AsyncMock(return_value={"vault_id": VAULT_ID})
    monkeypatch.setattr(event_tail_service, "check_vault_access", access)

    stream = event_tail_service._stream_events(
        _Request(),
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=("table.rows_changed",),
        position=9,
        codec=EventCursorCodec("cursor-secret"),
    )
    checkpoint = await anext(stream)
    change = await anext(stream)
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
    assert not connection.listeners
    assert access.await_count >= 2


@pytest.mark.asyncio
async def test_stream_closes_without_emitting_after_access_is_revoked(monkeypatch) -> None:
    from app.exceptions import ForbiddenError
    from app.services import event_tail_service

    class _Connection:
        async def add_listener(self, *_args: object) -> None:
            return None

        async def remove_listener(self, *_args: object) -> None:
            return None

    class _Acquire:
        async def __aenter__(self) -> _Connection:
            return _Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Pool:
        def acquire(self) -> _Acquire:
            return _Acquire()

    class _Request:
        async def is_disconnected(self) -> bool:
            return False

    async def get_pool() -> _Pool:
        return _Pool()

    monkeypatch.setattr(event_tail_service, "get_pool", get_pool)
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
    monkeypatch.setattr(
        event_tail_service,
        "check_vault_access",
        AsyncMock(side_effect=[{"vault_id": VAULT_ID}, ForbiddenError("revoked")]),
    )

    stream = event_tail_service._stream_events(
        _Request(),
        user_id="user",
        vault="vault",
        vault_id=VAULT_ID,
        kinds=(),
        position=0,
        codec=EventCursorCodec("cursor-secret"),
    )
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
