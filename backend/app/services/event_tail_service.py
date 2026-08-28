"""Authenticated PostgreSQL-backed Vault Change Event Tail."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, AsyncIterator, Mapping
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from fastapi import Request
from starlette.responses import StreamingResponse

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError
from app.models.events import ChangeEventEnvelopeV1, TailCheckpointV1
from app.repositories.events_repo import get_vault_event_bounds, list_vault_events
from app.services.access_service import check_vault_access


EVENT_CHANNEL = "akb_events"
EVENT_BATCH_SIZE = 100
HEARTBEAT_INTERVAL_SECONDS = 15.0
_CURSOR_PREFIX = "ec1."
_CURSOR_AAD = b"akb-event-cursor-v1"
_KIND_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[._:-][A-Za-z0-9]+)*$")


class EventCursorError(ValueError):
    """A malformed, unauthenticated, or wrongly scoped Event Cursor."""


class EventGapError(AKBError):
    """The requested cursor is older than the retained Vault tail."""

    def __init__(self, *, earliest_cursor: str, latest_cursor: str):
        super().__init__(
            "Event cursor is outside the retained Vault tail",
            status_code=410,
            code="event_gap",
            details={
                "earliest_cursor": earliest_cursor,
                "latest_cursor": latest_cursor,
            },
        )


@dataclass(frozen=True)
class EventBounds:
    """The retained event-id range for one Vault."""

    earliest_id: int | None
    latest_id: int | None


@dataclass(frozen=True)
class TailStart:
    """Resolved last-inspected position for a tail connection."""

    position: int


def _canonical_uuid(value: UUID | str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, ValueError, TypeError) as exc:
        raise EventCursorError("invalid event cursor") from exc


def _canonical_kinds(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise EventCursorError("invalid event cursor")
    canonical: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not _KIND_PATTERN.fullmatch(value):
            raise EventCursorError("invalid event cursor")
        canonical.add(value)
    return tuple(sorted(canonical))


def normalize_kind_filter(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Normalize a repeated exact-kind query into its cursor scope."""
    if values is None:
        return ()
    return _canonical_kinds(values)


class EventCursorCodec:
    """Encode and authenticate opaque cursors with the system HMAC secret.

    AES-SIV uses a key derived from ``system_hmac_secret_effective``. Its
    authentication tag provides the signature/integrity guarantee while the
    encrypted payload keeps the database position, Vault identity, and kind
    filter set out of the public cursor. Deterministic encryption keeps the
    same logical position stable across at-least-once redelivery.
    """

    def __init__(self, secret: str | None = None):
        effective = settings.system_hmac_secret_effective if secret is None else secret
        if not isinstance(effective, str) or not effective:
            raise ValueError("system HMAC secret is required for Event Cursors")
        self._key = hashlib.sha512(_CURSOR_AAD + b"\0" + effective.encode("utf-8")).digest()

    def encode(
        self,
        vault_id: UUID | str,
        kinds: tuple[str, ...] | list[str],
        position: int,
    ) -> str:
        vault_uuid = _canonical_uuid(vault_id)
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise EventCursorError("invalid event cursor")
        canonical_kinds = _canonical_kinds(kinds)
        payload = json.dumps(
            {
                "kinds": list(canonical_kinds),
                "position": position,
                "vault": str(vault_uuid),
                "version": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = AESSIV(self._key).encrypt(payload, [_CURSOR_AAD])
        return _CURSOR_PREFIX + _encode_bytes(encrypted)

    def decode(self, token: str) -> tuple[UUID, tuple[str, ...], int]:
        if not isinstance(token, str) or not token.startswith(_CURSOR_PREFIX) or len(token) > 4096:
            raise EventCursorError("invalid event cursor")
        encoded = token[len(_CURSOR_PREFIX) :]
        try:
            raw = _decode_bytes(encoded)
            if len(raw) <= 16:
                raise ValueError("cursor payload is empty")
            payload = AESSIV(self._key).decrypt(raw, [_CURSOR_AAD])
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or set(decoded) != {"kinds", "position", "vault", "version"}:
                raise ValueError("cursor payload shape is invalid")
            if decoded["version"] != 1:
                raise ValueError("cursor version is unsupported")
            vault_uuid = _canonical_uuid(decoded["vault"])
            position = decoded["position"]
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                raise ValueError("cursor position is invalid")
            kinds = _canonical_kinds(decoded["kinds"])
            if list(kinds) != decoded["kinds"]:
                raise ValueError("cursor kind scope is not canonical")
            return vault_uuid, kinds, position
        except EventCursorError:
            raise
        except Exception as exc:  # noqa: BLE001 - all decode failures are one public error
            raise EventCursorError("invalid event cursor") from exc


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("cursor encoding is invalid")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def resolve_start_position(
    codec: EventCursorCodec,
    vault_id: UUID | str,
    kinds: tuple[str, ...],
    bounds: EventBounds,
    *,
    last_event_id: str | None,
    cursor: str | None,
    start: str | None,
) -> TailStart:
    """Resolve the connection position using the public start precedence."""
    if start not in (None, "earliest"):
        raise EventCursorError("invalid event cursor")
    selected = last_event_id if last_event_id is not None else cursor
    if start == "earliest" and selected is not None:
        raise EventCursorError("invalid event cursor")

    if selected is not None:
        decoded_vault, decoded_kinds, position = codec.decode(selected)
        if decoded_vault != _canonical_uuid(vault_id) or decoded_kinds != kinds:
            raise EventCursorError("invalid event cursor")
        return TailStart(position=position)

    if start == "earliest":
        return TailStart(
            position=max(0, (bounds.earliest_id or 1) - 1),
        )
    return TailStart(position=bounds.latest_id or 0)


def validate_event_gap(
    codec: EventCursorCodec,
    vault_id: UUID | str,
    kinds: tuple[str, ...],
    *,
    position: int,
    bounds: EventBounds,
) -> None:
    """Reject a cursor that cannot reach the retained head without a gap."""
    earliest = bounds.earliest_id
    if earliest is None or position >= earliest - 1:
        return
    latest_position = bounds.latest_id if bounds.latest_id is not None else position
    raise EventGapError(
        earliest_cursor=codec.encode(vault_id, kinds, max(0, earliest - 1)),
        latest_cursor=codec.encode(vault_id, kinds, latest_position),
    )


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_envelope(row: Mapping[str, Any], *, cursor: str, vault: str) -> dict[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    envelope = ChangeEventEnvelopeV1(
        version=1,
        cursor=cursor,
        occurred_at=_as_utc(row["occurred_at"]),
        vault=vault,
        kind=str(row["kind"]),
        resource_uri=row.get("resource_uri"),
        actor=str(row["actor_id"]) if row.get("actor_id") is not None else None,
        payload=payload,
    )
    return envelope.model_dump(mode="json", exclude_none=True)


def format_sse(event_type: str, cursor: str, data: Mapping[str, Any]) -> str:
    """Render one SSE record with a JSON data line."""
    return (
        f"event: {event_type}\n"
        f"id: {cursor}\n"
        f"data: {json.dumps(dict(data), ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def format_heartbeat() -> str:
    """Render an SSE comment, which carries no Event Cursor."""
    return ": heartbeat\n\n"


async def open_tail(
    request: Request,
    *,
    user_id: str,
    vault: str,
    vault_id: UUID | str,
    last_event_id: str | None,
    cursor: str | None,
    start: str | None,
    kinds: list[str] | None,
) -> StreamingResponse:
    """Preflight and return a streaming response for one authorized Vault."""
    normalized_kinds = normalize_kind_filter(kinds)
    codec = EventCursorCodec()
    pool = await get_pool()
    async with pool.acquire() as conn:
        earliest_id, latest_id = await get_vault_event_bounds(conn, vault_id)
    bounds = EventBounds(earliest_id=earliest_id, latest_id=latest_id)
    resolved = resolve_start_position(
        codec,
        vault_id,
        normalized_kinds,
        bounds,
        last_event_id=last_event_id,
        cursor=cursor,
        start=start,
    )
    validate_event_gap(
        codec,
        vault_id,
        normalized_kinds,
        position=resolved.position,
        bounds=bounds,
    )
    stream = _stream_events(
        request,
        user_id=user_id,
        vault=vault,
        vault_id=vault_id,
        kinds=normalized_kinds,
        position=resolved.position,
        codec=codec,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_events(
    request: Request,
    *,
    user_id: str,
    vault: str,
    vault_id: UUID | str,
    kinds: tuple[str, ...],
    position: int,
    codec: EventCursorCodec,
) -> AsyncIterator[str]:
    async def still_authorized() -> bool:
        try:
            access = await check_vault_access(user_id, vault, required_role="reader")
            return str(access.get("vault_id")) == str(vault_id)
        except Exception:  # noqa: BLE001 - a revoked stream closes silently
            return False

    pool = await get_pool()
    async with pool.acquire() as conn:
        wakeup = asyncio.Event()

        def on_notify(*_args: Any) -> None:
            wakeup.set()

        await conn.add_listener(EVENT_CHANNEL, on_notify)
        try:
            current_position = position
            while True:
                if await request.is_disconnected():
                    return
                if not await still_authorized():
                    return

                wakeup.clear()
                rows = await list_vault_events(
                    conn,
                    vault_id,
                    after_id=current_position,
                    limit=EVENT_BATCH_SIZE,
                )
                if rows:
                    for row in rows:
                        if await request.is_disconnected():
                            return
                        if not await still_authorized():
                            return
                        current_position = int(row["id"])
                        event_cursor = codec.encode(vault_id, kinds, current_position)
                        if not kinds or row["kind"] in kinds:
                            yield format_sse(
                                "change",
                                event_cursor,
                                _event_envelope(row, cursor=event_cursor, vault=vault),
                            )
                        else:
                            checkpoint = TailCheckpointV1(version=1, cursor=event_cursor)
                            yield format_sse(
                                "checkpoint",
                                event_cursor,
                                checkpoint.model_dump(mode="json", exclude_none=True),
                            )
                    continue

                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
                except TimeoutError:
                    yield format_heartbeat()
        finally:
            await conn.remove_listener(EVENT_CHANNEL, on_notify)
