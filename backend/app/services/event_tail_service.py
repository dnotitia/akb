"""Authenticated PostgreSQL-backed Vault Change Event Tail.

Streams are long-lived and wait without a database connection. Everything a
stream does in the database (the open preflight, each batch read, each access
check) borrows one pooled connection within a per-process budget, so the
number of streams and the rate of events cannot take more of the pool than
that budget allows.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import threading
from typing import Any, AsyncIterator, Mapping
from uuid import UUID
import weakref

from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from fastapi import Request
from starlette.responses import StreamingResponse

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError
from app.models.events import ChangeEventEnvelopeV1, TailCheckpointV1
from app.repositories.events_repo import get_vault_event_bounds, list_vault_events
from app.services.access_service import check_vault_access
from app.services.event_tail_hub import event_tail_hub_connected, get_event_tail_hub


EVENT_BATCH_SIZE = 100
HEARTBEAT_INTERVAL_SECONDS = 15.0
# While a stream sends a batch, a passed access check covers the rows that
# follow it for this long. A reader slower than that is checked again before
# its next row, so it stops receiving rows about this long after losing access.
_RECHECK_DURING_YIELD_SECONDS = 1.0
# Each overrides the app.yaml key of the same name; see those settings.
MAX_STREAMS_ENV_VAR = "AKB_EVENT_TAIL_MAX_STREAMS"
MAX_STREAMS_PER_USER_ENV_VAR = "AKB_EVENT_TAIL_MAX_STREAMS_PER_USER"
DB_CONCURRENCY_ENV_VAR = "AKB_EVENT_TAIL_DB_CONCURRENCY"
# Slots free as streams end, which is not predictable; the write lane's
# saturation answer uses the same delay.
CAPACITY_RETRY_AFTER_SECONDS = 5
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


class EventTailCapacityError(AKBError):
    """This process already holds as many open streams as it is allowed."""

    def __init__(self, *, retry_after_secs: int = CAPACITY_RETRY_AFTER_SECONDS):
        # Surfaced as the HTTP Retry-After header by the AKBError handler.
        self.retry_after_secs = retry_after_secs
        super().__init__(
            "Event tail capacity is exhausted on this server",
            status_code=503,
            code="event_tail_capacity",
            hint="Retry after the Retry-After delay. Close event streams that are no longer read.",
        )


class EventTailUserQuotaError(AKBError):
    """This account already holds as many open streams as one account may.

    A 4xx rather than the 503 of a full process: the SDK's change channel
    retries a 5xx and stops on a 4xx, and a client over its own quota frees
    nothing by retrying.
    """

    def __init__(self, *, retry_after_secs: int = CAPACITY_RETRY_AFTER_SECONDS):
        # Surfaced as the HTTP Retry-After header by the AKBError handler.
        self.retry_after_secs = retry_after_secs
        super().__init__(
            "This account has too many open event streams on this server",
            status_code=429,
            code="event_tail_user_quota",
            hint="Close event streams this account no longer reads.",
        )


def _limit_from_env(name: str, configured: int) -> int:
    """The positive integer in environment variable ``name``, else ``configured``.

    Unset or blank means the app.yaml value. A malformed override fails
    loudly, as the pool-size overrides do, rather than falling back to a
    limit nobody chose.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return configured
    try:
        limit = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer; got {raw!r}") from None
    if limit < 1:
        raise RuntimeError(f"{name} must be at least 1; got {limit}")
    return limit


def configured_max_streams() -> int:
    """The per-process stream cap: AKB_EVENT_TAIL_MAX_STREAMS, else app.yaml."""
    return _limit_from_env(MAX_STREAMS_ENV_VAR, settings.event_tail_max_streams)


def configured_max_streams_per_user() -> int:
    """The per-account stream quota: AKB_EVENT_TAIL_MAX_STREAMS_PER_USER, else app.yaml."""
    return _limit_from_env(MAX_STREAMS_PER_USER_ENV_VAR, settings.event_tail_max_streams_per_user)


def configured_db_concurrency() -> int:
    """The tail's database budget: AKB_EVENT_TAIL_DB_CONCURRENCY, else app.yaml."""
    return _limit_from_env(DB_CONCURRENCY_ENV_VAR, settings.event_tail_db_concurrency)


class _StreamSlots:
    """How many event tail streams this process holds open, in all and per account.

    A slot can be given back from a garbage-collection callback: the
    ``weakref.finalize`` that open_tail registers for a body that never
    started. A collection runs on whichever thread is running when it falls
    due, and CPython can run one as soon as a call returns, the call that takes
    this lock included. With a plain lock, a slot given back there would wait
    on its own thread, and the event loop with it. So the lock is re-entrant,
    and acquire builds nothing while it holds it: a refusal's error is raised
    after the lock is let go.

    Re-entry cannot corrupt the counts. The code under the lock makes no calls,
    so a callback can run there only right after the lock is taken, before any
    count is read. That includes the per-account counts: they are read and
    written with subscripts on a dict keyed by ``str``, whose hashing and
    comparison run no Python code, and an account is removed from the dict when
    its last stream ends.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open = 0
        self._per_user: dict[str, int] = {}

    @property
    def open(self) -> int:
        return self._open

    @property
    def users(self) -> int:
        """Accounts holding at least one open stream."""
        return len(self._per_user)

    def acquire(self, user_id: str, *, limit: int, per_user_limit: int) -> _StreamSlot:
        """Admit one stream for ``user_id``, or refuse it.

        An account at its quota is refused with 429 even when the process is
        also full: closing its own streams is what lets it in, whatever else
        frees up.
        """
        user = str(user_id)
        per_user = self._per_user
        # The verdict is a plain int (0 admitted, 1 process full, 2 account at
        # its quota) so that deciding it makes no call.
        with self._lock:
            held = per_user[user] if user in per_user else 0
            if held >= per_user_limit:
                verdict = 2
            elif self._open >= limit:
                verdict = 1
            else:
                verdict = 0
                self._open += 1
                per_user[user] = held + 1
        if verdict == 2:
            raise EventTailUserQuotaError()
        if verdict == 1:
            raise EventTailCapacityError()
        return _StreamSlot(self, user)


class _StreamSlot:
    """One admitted stream. Releasing it again is a no-op."""

    __slots__ = ("_slots", "_user", "_held")

    def __init__(self, slots: _StreamSlots, user: str) -> None:
        self._slots = slots
        self._user = user
        self._held = True

    def release(self) -> None:
        slots = self._slots
        user = self._user
        per_user = slots._per_user
        with slots._lock:
            if not self._held:
                return
            self._held = False
            slots._open -= 1
            held = per_user[user] - 1
            if held:
                per_user[user] = held
            else:
                del per_user[user]


_stream_slots = _StreamSlots()


def open_stream_count() -> int:
    """Event tail streams this process currently holds open."""
    return _stream_slots.open


class _DbBudget:
    """The pool connections event tail work may hold at once in this process.

    A semaphore belongs to the event loop it first waits on, so a budget is
    made from the running loop on first use and made again for a caller on
    another loop; test runs start one loop per test. Its counters describe
    that loop's budget only.
    """

    __slots__ = ("loop", "size", "semaphore", "in_use", "waiters")

    def __init__(self, loop: asyncio.AbstractEventLoop, size: int) -> None:
        self.loop = loop
        self.size = size
        self.semaphore = asyncio.Semaphore(size)
        # Holders of a slot, whether still waiting for the pool or holding a
        # connection, and coroutines waiting for a slot.
        self.in_use = 0
        self.waiters = 0


_db_budget: _DbBudget | None = None


def _current_db_budget() -> _DbBudget:
    global _db_budget
    loop = asyncio.get_running_loop()
    budget = _db_budget
    if budget is None or budget.loop is not loop:
        budget = _DbBudget(loop, configured_db_concurrency())
        _db_budget = budget
    return budget


@contextlib.asynccontextmanager
async def _tail_db_connection() -> AsyncIterator[Any]:
    """Borrow one pool connection within the event tail's database budget.

    Waiting for a slot holds nothing, and a slot is held only until the
    connection goes back, so event tail work never holds more pool
    connections than the budget, however many streams are woken at once.
    """
    budget = _current_db_budget()
    budget.waiters += 1
    try:
        await budget.semaphore.acquire()
    finally:
        budget.waiters -= 1
    budget.in_use += 1
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            yield conn
    finally:
        budget.in_use -= 1
        budget.semaphore.release()


def stats_snapshot() -> dict[str, int | bool]:
    """This process's event tail counters, for /health.

    Counts and limits only: nothing here names an account or a Vault, so the
    section is safe on the unauthenticated half of /health.
    """
    budget = _db_budget
    return {
        "open_streams": _stream_slots.open,
        "accounts_with_streams": _stream_slots.users,
        "max_streams": configured_max_streams(),
        "max_streams_per_user": configured_max_streams_per_user(),
        "budget_size": budget.size if budget is not None else configured_db_concurrency(),
        "budget_in_use": budget.in_use if budget is not None else 0,
        "budget_waiters": budget.waiters if budget is not None else 0,
        "hub_connected": event_tail_hub_connected(),
    }


@dataclass(frozen=True)
class EventBounds:
    """The retained event-id range for one Vault."""

    earliest_id: int | None
    latest_id: int | None


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

    AES-SIV uses a key derived from ``system_hmac_secret``. Its
    authentication tag provides the signature/integrity guarantee while the
    encrypted payload keeps the database position, Vault identity, and kind
    filter set out of the public cursor. Deterministic encryption keeps the
    same logical position stable across at-least-once redelivery.
    """

    def __init__(self, secret: str | None = None):
        effective = settings.system_hmac_secret if secret is None else secret
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
) -> int:
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
        return position

    if start == "earliest":
        return max(0, (bounds.earliest_id or 1) - 1)
    return bounds.latest_id or 0


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
    """Preflight and return a streaming response for one authorized Vault.

    Admission comes first: an account at its quota is refused with 429, and a
    process at its stream cap with 503, before the preflight reads the
    database.
    """
    slot = _stream_slots.acquire(
        user_id,
        limit=configured_max_streams(),
        per_user_limit=configured_max_streams_per_user(),
    )
    try:
        normalized_kinds = normalize_kind_filter(kinds)
        codec = EventCursorCodec()
        async with _tail_db_connection() as conn:
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
            position=resolved,
            bounds=bounds,
        )
        stream = _stream_events(
            request,
            user_id=user_id,
            vault=vault,
            vault_id=vault_id,
            kinds=normalized_kinds,
            position=resolved,
            codec=codec,
            slot=slot,
        )
        # The generator gives the slot back in its `finally`, but only once it
        # has started. If the client leaves before the body is first iterated,
        # it never starts; give the slot back when it is collected instead.
        weakref.finalize(stream, slot.release)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except BaseException:
        slot.release()
        raise


class _AccessLost(Exception):
    """The stream's reader may no longer read its Vault; the stream ends quietly."""


async def _require_access(conn: Any, *, user_id: str, vault: str, vault_id: UUID | str) -> None:
    """Check reader access on ``conn``, raising _AccessLost if it is gone."""
    try:
        access = await check_vault_access(user_id, vault, required_role="reader", _conn=conn)
    except Exception as exc:  # noqa: BLE001 - a revoked stream closes silently
        raise _AccessLost from exc
    # A Vault deleted and created again under the same name is another Vault.
    if str(access.get("vault_id")) != str(vault_id):
        raise _AccessLost


async def _read_pass(
    *,
    user_id: str,
    vault: str,
    vault_id: UUID | str,
    after_id: int,
    check_when_empty: bool,
) -> tuple[list[dict[str, Any]], float | None]:
    """Read the next batch and check access, both on one borrowed connection.

    The check runs after the read, so it covers every row read, and only when
    the batch has rows or ``check_when_empty`` asks for it: an empty pass costs
    one query. Returns the rows and the loop time the check started, or None
    when no check ran; raises _AccessLost when the check fails. The connection
    is back in the pool before the caller sends anything.
    """
    async with _tail_db_connection() as conn:
        rows = await list_vault_events(conn, vault_id, after_id=after_id, limit=EVENT_BATCH_SIZE)
        if not rows and not check_when_empty:
            return rows, None
        checked_at = asyncio.get_running_loop().time()
        await _require_access(conn, user_id=user_id, vault=vault, vault_id=vault_id)
    return rows, checked_at


async def _recheck_access(*, user_id: str, vault: str, vault_id: UUID | str) -> float:
    """Check access again on a borrowed connection; returns the loop time it started."""
    async with _tail_db_connection() as conn:
        checked_at = asyncio.get_running_loop().time()
        await _require_access(conn, user_id=user_id, vault=vault, vault_id=vault_id)
    return checked_at


async def _stream_events(
    request: Request,
    *,
    user_id: str,
    vault: str,
    vault_id: UUID | str,
    kinds: tuple[str, ...],
    position: int,
    codec: EventCursorCodec,
    slot: _StreamSlot | None = None,
) -> AsyncIterator[str]:
    """Send ``vault_id``'s events after ``position`` until the client leaves or loses access.

    Each pass borrows one pooled connection, within the database budget, to
    read the next batch and, when it found rows, to check access; the
    connection goes back before any frame is sent. A pass that finds nothing
    checks nothing, except the pass right after a heartbeat, so a stream that
    receives no rows still closes within one heartbeat of losing access.

    Invariant: no row is yielded unless an access check that started after the
    row was read has passed. While a batch is sent, a check that started more
    than _RECHECK_DURING_YIELD_SECONDS earlier is repeated before the next row,
    so a reader slower than that stops receiving rows within about that long
    of losing access.
    """
    try:
        # Wake-ups come from the process-wide LISTEN hub, for this Vault's
        # events only. Rows are read from the events table by cursor, so a
        # missed wake-up only delays this stream until its heartbeat re-read.
        hub = get_event_tail_hub()
        subscription = hub.subscribe(vault_id)
        try:
            wakeup = subscription.wakeup
            loop = asyncio.get_running_loop()
            last_frame_at = loop.time()
            current_position = position
            # When the last passed access check started. Every pass that reads
            # rows checks, so this is set before any row is sent; -inf would
            # force a check if it were not.
            checked_at = -math.inf
            check_when_empty = False
            while True:
                if await request.is_disconnected():
                    return

                wakeup.clear()
                try:
                    rows, pass_checked_at = await _read_pass(
                        user_id=user_id,
                        vault=vault,
                        vault_id=vault_id,
                        after_id=current_position,
                        check_when_empty=check_when_empty,
                    )
                except _AccessLost:
                    return
                check_when_empty = False
                if pass_checked_at is not None:
                    checked_at = pass_checked_at
                if rows:
                    for row in rows:
                        if await request.is_disconnected():
                            return
                        if loop.time() - checked_at > _RECHECK_DURING_YIELD_SECONDS:
                            try:
                                checked_at = await _recheck_access(
                                    user_id=user_id,
                                    vault=vault,
                                    vault_id=vault_id,
                                )
                            except _AccessLost:
                                return
                        current_position = int(row["id"])
                        event_cursor = codec.encode(vault_id, kinds, current_position)
                        if not kinds or row["kind"] in kinds:
                            frame = format_sse(
                                "change",
                                event_cursor,
                                _event_envelope(row, cursor=event_cursor, vault=vault),
                            )
                            yield frame
                            last_frame_at = loop.time()
                        else:
                            checkpoint = TailCheckpointV1(version=1, cursor=event_cursor)
                            frame = format_sse(
                                "checkpoint",
                                event_cursor,
                                checkpoint.model_dump(mode="json", exclude_none=True),
                            )
                            yield frame
                            last_frame_at = loop.time()
                    continue

                remaining = max(
                    0.0,
                    HEARTBEAT_INTERVAL_SECONDS - (loop.time() - last_frame_at),
                )
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=remaining)
                except TimeoutError:
                    yield format_heartbeat()
                    last_frame_at = loop.time()
                    # A stream with no new rows checks nothing on its own, so
                    # the pass after each heartbeat checks access regardless.
                    check_when_empty = True
        finally:
            hub.unsubscribe(subscription)
    finally:
        if slot is not None:
            slot.release()
