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
import collections
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import re
import threading
from typing import Any, AsyncGenerator, AsyncIterator, Mapping
from uuid import UUID
import weakref

from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from fastapi import Request
from starlette.responses import StreamingResponse

from app.config import settings
from app.db.postgres import get_pool
from app.db.transient import transient_error
from app.exceptions import AKBError
from app.models.events import ChangeEventEnvelopeV1, TailCheckpointV1
from app.repositories.events_repo import get_vault_event_bounds, list_vault_events
from app.services.access_service import check_vault_access
from app.services.event_tail_hub import event_tail_hub_connected, get_event_tail_hub

logger = logging.getLogger("akb.event_tail")

EVENT_BATCH_SIZE = 100
HEARTBEAT_INTERVAL_SECONDS = 15.0
# While a stream sends a batch, a passed access check covers the rows that
# follow it for this long from when it started. A reader slower than that is
# checked again before its next row, so it stops receiving rows about this
# long after losing access.
_RECHECK_DURING_YIELD_SECONDS = 1.0
# A check that took longer than that window has expired by the time it
# returns. After this many such checks in a row the stream ends; its client
# reconnects and resumes from the last cursor it received.
_MAX_STALE_ACCESS_CHECKS = 3
# How long a connection may take to go back to the pool. asyncpg waits for a
# cancelled query to stop and then resets the connection; a half-open
# connection may never answer. Past the deadline asyncpg closes it instead,
# which gives its place in the pool back.
_RETURN_DEADLINE_SECONDS = 5.0
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


class EventTailUnavailableError(AKBError):
    """Access could not be confirmed in time to start a stream.

    The access checks kept outlasting the window they cover, as on a slow
    database. A 5xx: the SDK's change channel retries it.
    """

    def __init__(self, *, retry_after_secs: int = 1):
        # Surfaced as the HTTP Retry-After header by the AKBError handler.
        self.retry_after_secs = retry_after_secs
        super().__init__(
            "Event tail access could not be confirmed in time",
            status_code=503,
            code="event_tail_unavailable",
            hint="Retry after the Retry-After delay.",
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

    A stream whose client leaves before its body starts is given back by the
    ``weakref.finalize`` that open_tail registers, from inside whatever garbage
    collection frees the body. A collection runs on whichever thread is
    running when it falls due, at almost any point: as soon as a call returns,
    the call that takes this lock included, or between two lines when a trace
    function (a debugger, a profiler) runs. A slot given back there must not
    wait for the lock its own thread may hold, and must not change a count
    that the code it interrupted has read and is about to write.

    So the finalizer only appends the slot to ``_abandoned``. A deque's append
    takes no lock and runs no Python code. Every acquire, release and count
    first gives back the queued slots under the lock, so the counts change
    only in code that holds the lock, and nothing interrupting that code
    changes them. The lock stays re-entrant, although nothing relies on that
    any more, and acquire builds nothing while it holds it: a refusal's error
    is raised after the lock is let go.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open = 0
        self._per_user: dict[str, int] = {}
        # Slots that finalizers gave back and no count reflects yet.
        self._abandoned: collections.deque[_StreamSlot] = collections.deque()

    @property
    def open(self) -> int:
        with self._lock:
            self._settle_abandoned()
            return self._open

    @property
    def users(self) -> int:
        """Accounts holding at least one open stream."""
        with self._lock:
            self._settle_abandoned()
            return len(self._per_user)

    def acquire(self, user_id: str, *, limit: int, per_user_limit: int) -> _StreamSlot:
        """Admit one stream for ``user_id``, or refuse it.

        An account at its quota is refused with 429 even when the process is
        also full: closing its own streams is what lets it in, whatever else
        frees up.
        """
        user = str(user_id)
        # 0 admitted, 1 process full, 2 account at its quota.
        with self._lock:
            self._settle_abandoned()
            held = self._per_user.get(user, 0)
            if held >= per_user_limit:
                verdict = 2
            elif self._open >= limit:
                verdict = 1
            else:
                verdict = 0
                self._open += 1
                self._per_user[user] = held + 1
        if verdict == 2:
            raise EventTailUserQuotaError()
        if verdict == 1:
            raise EventTailCapacityError()
        return _StreamSlot(self, user)

    def _settle_abandoned(self) -> None:
        """Give back the slots finalizers queued. Call with the lock held."""
        abandoned = self._abandoned
        while abandoned:
            abandoned.popleft()._give_back()


class _StreamSlot:
    """One admitted stream. Giving it back more than once counts once."""

    __slots__ = ("_slots", "_user", "_held")

    def __init__(self, slots: _StreamSlots, user: str) -> None:
        self._slots = slots
        self._user = user
        self._held = True

    def release(self) -> None:
        """Give the slot back now. Call from ordinary code, not a finalizer."""
        slots = self._slots
        with slots._lock:
            slots._settle_abandoned()
            self._give_back()

    def abandon(self) -> None:
        """Queue the slot to be given back; safe from a finalizer on any thread."""
        self._slots._abandoned.append(self)

    def _give_back(self) -> None:
        """Uncount the slot unless it already was. Call with the lock held."""
        if not self._held:
            return
        self._held = False
        slots = self._slots
        slots._open -= 1
        held = slots._per_user[self._user] - 1
        if held:
            slots._per_user[self._user] = held
        else:
            del slots._per_user[self._user]


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

    def give_back(self) -> None:
        self.in_use -= 1
        self.semaphore.release()


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

    Waiting for a slot holds nothing. A slot is given back only once its
    connection is back in the pool, or closed. asyncpg returns a connection in
    a shielded task, which goes on when the borrower is cancelled while it
    waits for that return, as a stream is when its client leaves. The slot is
    therefore given back when that return finishes, not when the borrower's
    cleanup runs, and the return has a deadline, so a connection that never
    comes back cannot keep a slot either. Event tail work never holds more
    pool connections than the budget, however many streams are woken or
    cancelled at once.
    """
    budget = _current_db_budget()
    budget.waiters += 1
    try:
        await budget.semaphore.acquire()
    finally:
        budget.waiters -= 1
    budget.in_use += 1
    returning: asyncio.Future[None] | None = None
    try:
        pool = await get_pool()
        conn = await pool.acquire()
        try:
            yield conn
        finally:
            returning = asyncio.ensure_future(_return_connection(pool, conn))
            returning.add_done_callback(lambda _returned: budget.give_back())
            await asyncio.shield(returning)
    finally:
        if returning is None:
            budget.give_back()


async def _return_connection(pool: Any, conn: Any) -> None:
    """Give ``conn`` back to ``pool`` within _RETURN_DEADLINE_SECONDS.

    Past the deadline, or when the return fails, asyncpg closes the connection,
    which also gives its place in the pool back. The borrower's work is done
    either way, so the failure is logged rather than raised.
    """
    try:
        await pool.release(conn, timeout=_RETURN_DEADLINE_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the connection is closed; nothing is left to undo
        logger.warning(
            "An event tail connection did not return to the pool (%s); it was closed instead",
            type(exc).__name__,
        )


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
    """The retained event-id range for one Vault, and how far retention deleted.

    ``deleted_through`` is the highest id of the Vault's events that retention
    has deleted, 0 if none. Event ids are shared by every Vault, so the range
    alone cannot tell a cursor that missed a deleted event from one taken
    before the Vault's first event.
    """

    earliest_id: int | None
    latest_id: int | None
    deleted_through: int = 0


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
        # Just before the earliest retained event, unless retention deleted
        # an event after it: then from there, the earliest position whose
        # tail is complete.
        return max(bounds.deleted_through, (bounds.earliest_id or 1) - 1)
    return max(bounds.latest_id or 0, bounds.deleted_through)


def validate_event_gap(
    codec: EventCursorCodec,
    vault_id: UUID | str,
    kinds: tuple[str, ...],
    *,
    position: int,
    bounds: EventBounds,
) -> None:
    """Reject a cursor that never received an event retention has since deleted.

    The Vault's watermark is the highest id it deleted, so a cursor below it
    missed that event. A cursor at or above it missed nothing, however far
    below the Vault's first retained event it is. The refusal's earliest
    cursor resumes at the watermark, after which nothing was deleted.
    """
    if position >= bounds.deleted_through:
        return
    latest_position = max(bounds.latest_id or 0, bounds.deleted_through)
    raise EventGapError(
        earliest_cursor=codec.encode(vault_id, kinds, bounds.deleted_through),
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
    last_event_id: str | None,
    cursor: str | None,
    start: str | None,
    kinds: list[str] | None,
) -> StreamingResponse:
    """Check access, preflight, and return a streaming response for one Vault.

    Admission is decided first and reads nothing. Then one connection, within
    the database budget, serves the access check and, after it, the read of
    the Vault's retained range, so a burst of reconnects holds no more
    connections than the budget. The access check comes before any event
    query.

    The stream's first frame, a checkpoint at its start position, is built
    here, on that access check, so every client holds a cursor before
    anything else is sent. It is part of the response to this authorized
    request: it carries no event, only the position computed under the
    check, and goes out first however long the response takes to start. If
    the preflight outlasted the window the check covers, access is checked
    again here; if the checks stay slow, the request fails with a retryable
    503 `event_tail_unavailable`.

    The errors keep the order the route has always given them: 403, or 404
    for an unknown Vault, before 429 for an account at its quota and 503 for
    a full process; then 400 for an invalid kind or cursor, and 410 for a
    cursor that missed a deleted event. A refused request is therefore still
    checked, within the budget, so a client without access is told so, and
    stops, rather than told to retry.
    """
    try:
        slot = _stream_slots.acquire(
            user_id,
            limit=configured_max_streams(),
            per_user_limit=configured_max_streams_per_user(),
        )
    except (EventTailUserQuotaError, EventTailCapacityError):
        # A caller without access hears that first, and does not retry it.
        async with _tail_db_connection() as conn:
            await check_vault_access(user_id, vault, required_role="reader", _conn=conn)
        raise
    try:
        async with _tail_db_connection() as conn:
            checked_at = asyncio.get_running_loop().time()
            access = await check_vault_access(user_id, vault, required_role="reader", _conn=conn)
            normalized_kinds = normalize_kind_filter(kinds)
            vault_id = access["vault_id"]
            earliest_id, latest_id, deleted_through = await get_vault_event_bounds(conn, vault_id)
        codec = EventCursorCodec()
        bounds = EventBounds(earliest_id=earliest_id, latest_id=latest_id, deleted_through=deleted_through)
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
        start_frame = _checkpoint_frame(codec.encode(vault_id, normalized_kinds, resolved))
        checked_at = await _confirm_start(checked_at, user_id=user_id, vault=vault, vault_id=vault_id)
        stream = _stream_events(
            request,
            user_id=user_id,
            vault=vault,
            vault_id=vault_id,
            kinds=normalized_kinds,
            position=resolved,
            codec=codec,
            start_frame=start_frame,
            checked_at=checked_at,
            slot=slot,
        )
        # The generator gives the slot back in its `finally`, but only once it
        # has started. If the client leaves before the body is first iterated,
        # it never starts; give the slot back when it is collected instead.
        weakref.finalize(stream, slot.abandon)
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


class _CheckFailed(Exception):
    """An access check could not be made, as when the database connection was lost.

    It shows nothing either way about access, so it counts as a slow check:
    retried before a frame, up to _MAX_STALE_ACCESS_CHECKS in a row. ``rows``
    are the rows its pass read, still to be sent, and ``attempted_at`` the
    loop time the check started.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.attempted_at = -math.inf


async def _confirm_start(checked_at: float, *, user_id: str, vault: str, vault_id: UUID | str) -> float:
    """Keep an access check covering the start checkpoint; returns when that check started.

    open_tail's check, started at ``checked_at`` (loop time), covers the start
    checkpoint for _RECHECK_DURING_YIELD_SECONDS. If the preflight outlasted
    that, access is checked again, up to _MAX_STALE_ACCESS_CHECKS slow checks in
    all, the expired one included. A failed check raises its own error (403,
    404). Checks that stay slow, or a Vault replaced meanwhile, raise
    EventTailUnavailableError, which the client retries.
    """
    loop = asyncio.get_running_loop()
    if loop.time() - checked_at <= _RECHECK_DURING_YIELD_SECONDS:
        return checked_at
    for _ in range(_MAX_STALE_ACCESS_CHECKS - 1):
        async with _tail_db_connection() as conn:
            checked_at = loop.time()
            access = await check_vault_access(user_id, vault, required_role="reader", _conn=conn)
        if str(access.get("vault_id")) != str(vault_id):
            raise EventTailUnavailableError()
        if loop.time() - checked_at <= _RECHECK_DURING_YIELD_SECONDS:
            return checked_at
    raise EventTailUnavailableError()


async def _require_access(conn: Any, *, user_id: str, vault: str, vault_id: UUID | str) -> None:
    """Check reader access on ``conn``.

    Raises _AccessLost when access is gone, and _CheckFailed when a transient
    database fault kept the check from being made. Any other error counts as
    access lost: the stream ends rather than send on a check it cannot read.
    """
    try:
        access = await check_vault_access(user_id, vault, required_role="reader", _conn=conn)
    except Exception as exc:  # noqa: BLE001 - a revoked stream closes silently
        if transient_error(exc) is not None:
            raise _CheckFailed from exc
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
    when no check ran; raises _AccessLost when access is gone, and _CheckFailed,
    carrying the rows, when the check could not be made. The connection is
    back in the pool before the caller sends anything.
    """
    async with _tail_db_connection() as conn:
        rows = await list_vault_events(conn, vault_id, after_id=after_id, limit=EVENT_BATCH_SIZE)
        if not rows and not check_when_empty:
            return rows, None
        checked_at = asyncio.get_running_loop().time()
        try:
            await _require_access(conn, user_id=user_id, vault=vault, vault_id=vault_id)
        except _CheckFailed as failed:
            failed.rows = rows
            failed.attempted_at = checked_at
            raise
    return rows, checked_at


async def _recheck_access(*, user_id: str, vault: str, vault_id: UUID | str) -> float:
    """Check access again on a borrowed connection; returns the loop time it started."""
    async with _tail_db_connection() as conn:
        checked_at = asyncio.get_running_loop().time()
        try:
            await _require_access(conn, user_id=user_id, vault=vault, vault_id=vault_id)
        except _CheckFailed as failed:
            failed.attempted_at = checked_at
            raise
    return checked_at


def _outcome_seen(task: asyncio.Future[Any]) -> None:
    """Mark a check's outcome as read: the stream may end before it reads it."""
    if not task.cancelled():
        task.exception()


def _checkpoint_frame(cursor: str) -> str:
    checkpoint = TailCheckpointV1(version=1, cursor=cursor)
    return format_sse("checkpoint", cursor, checkpoint.model_dump(mode="json", exclude_none=True))


def _row_frame(
    row: Mapping[str, Any],
    *,
    codec: EventCursorCodec,
    vault: str,
    vault_id: UUID | str,
    kinds: tuple[str, ...],
) -> str:
    """The frame for one row: its change, or a checkpoint when its kind is filtered out."""
    cursor = codec.encode(vault_id, kinds, int(row["id"]))
    if not kinds or row["kind"] in kinds:
        return format_sse("change", cursor, _event_envelope(row, cursor=cursor, vault=vault))
    return _checkpoint_frame(cursor)


class _StreamClock:
    """The times a stream's access checks and heartbeats are measured from."""

    __slots__ = ("attempted_at", "checked_at", "check_covered_a_frame", "last_frame_at", "last_heartbeat_at")

    def __init__(self, *, checked_at: float, now: float) -> None:
        # When the last passed access check started, and whether a frame has
        # been sent on it since. Only a passed check covers rows.
        self.checked_at = checked_at
        self.check_covered_a_frame = False
        # When the last check started, passed or not: one that a database
        # fault kept from being made was still attempted.
        self.attempted_at = checked_at
        # When the last frame, a heartbeat included, was sent, and the last heartbeat.
        self.last_frame_at = now
        self.last_heartbeat_at = -math.inf

    @property
    def check_owed(self) -> bool:
        """Whether a heartbeat went out after the last access check was attempted.

        A stream that receives no rows checks nothing on its own, so after each
        heartbeat it owes one check, even when the batch it reads is empty. A
        check that could not be made settles that heartbeat's: it is tried
        again after the next heartbeat, not at once.
        """
        return self.attempted_at < self.last_heartbeat_at


async def _heartbeats_while(task: asyncio.Future[Any], clock: _StreamClock) -> AsyncGenerator[str, None]:
    """Yield a heartbeat each time one falls due, until ``task`` is done.

    A heartbeat counts as sent once the stream asks for its next frame, as the
    heartbeat of the stream's own wait does. If the stream ends first, the
    task is cancelled.
    """
    loop = asyncio.get_running_loop()
    try:
        while not task.done():
            due_in = HEARTBEAT_INTERVAL_SECONDS - (loop.time() - clock.last_frame_at)
            if due_in > 0:
                await asyncio.wait({task}, timeout=due_in)
                continue
            yield format_heartbeat()
            clock.last_frame_at = clock.last_heartbeat_at = loop.time()
    finally:
        if not task.done():
            task.cancel()


async def _renew_access(
    clock: _StreamClock,
    *,
    user_id: str,
    vault: str,
    vault_id: UUID | str,
) -> AsyncGenerator[str, None]:
    """Check access until a check covers the next frame, yielding heartbeats meanwhile.

    Call when the current check started more than _RECHECK_DURING_YIELD_SECONDS
    ago. A check that expired before it covered a frame was itself slow, and so
    is each re-check that outlasts the window or could not be made. After
    _MAX_STALE_ACCESS_CHECKS slow checks in a row, or when access is gone,
    raises _AccessLost: the stream ends with no further frame, and its client
    resumes from the last cursor it received, its start checkpoint at least.
    """
    loop = asyncio.get_running_loop()
    slow_checks = 0 if clock.check_covered_a_frame else 1
    while slow_checks < _MAX_STALE_ACCESS_CHECKS:
        recheck = asyncio.ensure_future(_recheck_access(user_id=user_id, vault=vault, vault_id=vault_id))
        recheck.add_done_callback(_outcome_seen)
        async with contextlib.aclosing(_heartbeats_while(recheck, clock)) as heartbeats:
            async for heartbeat in heartbeats:
                yield heartbeat
        try:
            clock.checked_at = recheck.result()
        except _CheckFailed as failed:
            clock.attempted_at = failed.attempted_at
            slow_checks += 1
            continue
        clock.attempted_at = clock.checked_at
        clock.check_covered_a_frame = False
        if loop.time() - clock.checked_at <= _RECHECK_DURING_YIELD_SECONDS:
            return
        slow_checks += 1
    raise _AccessLost


async def _stream_events(
    request: Request,
    *,
    user_id: str,
    vault: str,
    vault_id: UUID | str,
    kinds: tuple[str, ...],
    position: int,
    codec: EventCursorCodec,
    start_frame: str,
    checked_at: float,
    slot: _StreamSlot | None = None,
) -> AsyncIterator[str]:
    """Send ``vault_id``'s events after ``position`` until the client leaves or loses access.

    The first frame is ``start_frame``, the checkpoint at ``position`` that
    open_tail built on an access check started at ``checked_at`` (loop time),
    so every client holds a cursor to resume from whether or not it brought
    one. It is part of the response to the authorized open request: it
    carries no event, only the position computed under that check, so it
    goes out first, before anything is awaited and however long the
    response's start took, with no further check.

    Each pass borrows one pooled connection, within the database budget, to
    read the next batch and, when it found rows, to check access; the
    connection goes back before any frame is sent. Every row is therefore
    sent on a check made after it was read, so a reader whose access was
    revoked while the response started gets the checkpoint and no event. A
    pass that finds nothing checks nothing, unless a heartbeat went out since
    the last check: then it checks. Each heartbeat that goes out while a pass
    runs, after its check, calls for one more check before the stream waits.
    A stream that receives no rows therefore closes within one heartbeat of
    losing access. Heartbeats keep their interval while a pass or a check
    runs. A check that a transient database fault keeps from being made counts
    as a slow check before the rows it should cover; an idle stream makes it
    again after the next heartbeat.

    Invariant: no frame other than a heartbeat or the start checkpoint is
    yielded unless an access check has passed that started no more than
    _RECHECK_DURING_YIELD_SECONDS before the frame is yielded and after the
    frame's row was read. Freshness is judged once the frame is built, right
    before it is sent; _renew_access says what happens when the check has
    expired. A stream whose access cannot be confirmed ends with no further
    frame.
    """
    try:
        yield start_frame
        # Wake-ups come from the process-wide LISTEN hub, for this Vault's
        # events only. Rows are read from the events table by cursor, so a
        # missed wake-up only delays this stream until its heartbeat re-read.
        hub = get_event_tail_hub()
        subscription = hub.subscribe(vault_id)
        try:
            wakeup = subscription.wakeup
            loop = asyncio.get_running_loop()
            clock = _StreamClock(checked_at=checked_at, now=loop.time())
            clock.check_covered_a_frame = True
            current_position = position

            while True:
                if await request.is_disconnected():
                    return

                wakeup.clear()
                read = asyncio.ensure_future(
                    _read_pass(
                        user_id=user_id,
                        vault=vault,
                        vault_id=vault_id,
                        after_id=current_position,
                        check_when_empty=clock.check_owed,
                    )
                )
                read.add_done_callback(_outcome_seen)
                async with contextlib.aclosing(_heartbeats_while(read, clock)) as heartbeats:
                    async for heartbeat in heartbeats:
                        yield heartbeat
                try:
                    rows, pass_checked_at = read.result()
                except _AccessLost:
                    return
                except _CheckFailed as failed:
                    rows, pass_checked_at = failed.rows, None
                    clock.attempted_at = failed.attempted_at
                    if rows:
                        # No check covers these rows. Each is renewed before it
                        # is sent, this failure counting as the first slow check.
                        clock.checked_at = -math.inf
                        clock.check_covered_a_frame = False
                if pass_checked_at is not None:
                    clock.checked_at = clock.attempted_at = pass_checked_at
                    clock.check_covered_a_frame = False

                for row in rows:
                    if await request.is_disconnected():
                        return
                    frame = _row_frame(row, codec=codec, vault=vault, vault_id=vault_id, kinds=kinds)
                    if loop.time() - clock.checked_at > _RECHECK_DURING_YIELD_SECONDS:
                        try:
                            async with contextlib.aclosing(
                                _renew_access(clock, user_id=user_id, vault=vault, vault_id=vault_id)
                            ) as heartbeats:
                                async for heartbeat in heartbeats:
                                    yield heartbeat
                        except _AccessLost:
                            return
                    yield frame
                    current_position = int(row["id"])
                    clock.last_frame_at = loop.time()
                    clock.check_covered_a_frame = True
                if rows:
                    continue
                if clock.check_owed:
                    # A heartbeat went out while this pass ran, after the last
                    # check was attempted: check now rather than a heartbeat
                    # later. Each attempt settles the heartbeats before it,
                    # passed or not, so there is one per heartbeat, and a
                    # database slower than the heartbeat interval or failing
                    # cannot loop here.
                    continue

                remaining = max(
                    0.0,
                    HEARTBEAT_INTERVAL_SECONDS - (loop.time() - clock.last_frame_at),
                )
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=remaining)
                except TimeoutError:
                    yield format_heartbeat()
                    # The pass after it owes a check, even if it finds nothing.
                    clock.last_frame_at = clock.last_heartbeat_at = loop.time()
        finally:
            hub.unsubscribe(subscription)
    finally:
        if slot is not None:
            slot.release()
