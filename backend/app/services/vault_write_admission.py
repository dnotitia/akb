"""Per-process admission for transactions that write a Vault's events.

Migration 132 makes the event-writing transactions of one Vault run one at a
time on the Vault's event lock. A transaction that waits for that lock keeps
its pool connection for up to 5 seconds, and a Bare Git document write holds
the lock through its Git work. When one Vault receives more such writes than
it can commit one at a time, the waiters took every connection of the pool,
and requests of other Vaults, and the readiness check, waited for one.

Every transaction that may write an event for a Vault therefore enters
``vault_write_admission`` for that Vault before it asks the pool for a
connection. At most ``vault_write_concurrency`` transactions of one Vault are
admitted at a time in this process; the others wait as suspended coroutines
that hold no connection. A wait longer than
``vault_write_queue_timeout_secs`` raises ``WriteBusyError`` (HTTP 429 with
``Retry-After``, MCP ``write_busy``) before the transaction, so retrying it is
safe; so does a request that finds ``vault_write_max_waiters`` transactions
already waiting, at once. The limit is per process: with several API
processes a Vault can have that many admitted transactions in each, and the
event lock still orders them.

Admission is re-entrant within a task: a call nested in an admitted one for
the same Vault does not wait again. A transaction that writes events for
several Vaults is admitted for all of them at once, under one deadline, and
holds none of them while it waits. Waiters are admitted first come, first
served, by capacity: the first one waiting on a Vault reserves one slot of it
against later transactions, so a full Vault holds back another Vault's
writes by at most the one slot that transactions waiting for both reserve
there.
Code inside an admitted transaction must not wait for another
admission, and must not take the Bare Git write lane, which is entered before
this admission. Vault creation takes the new Vault's write-lane gate (not a
global lane slot) and then its admission and holds both through its seed
document, whose write re-enters both.

A transaction takes its connection with ``admitted_connection``, which gives
it back to the pool before the admission. Code that runs inside an admission
entered without a connection (a Native mutation admitted before it prepares
its payload, and Vault creation) takes each connection with
``acquire_given_back``, which does the same before the code after it runs.

A write that resolves its Vault's name to an id for admission checks, in its
transaction, that the name still names that Vault (``check_admitted_vault``);
``retry_if_vault_replaced`` then runs it once more for the Vault it found.

``emit_event`` and ``lock_vault_events`` log, once per event kind, an event
written for a Vault outside its admission; that names a path that skips it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, ParamSpec, TypeVar

import asyncpg

from app.exceptions import ConflictError, WriteBusyError

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger("akb.vault_write_admission")

# Vaults the current task is admitted for.
_admitted: ContextVar[frozenset[uuid.UUID]] = ContextVar(
    "akb_vault_write_admitted", default=frozenset(),
)


class _Waiter:
    __slots__ = ("vault_ids", "granted")

    def __init__(self, vault_ids: list[uuid.UUID], granted: asyncio.Future) -> None:
        self.vault_ids = vault_ids
        self.granted = granted


class _State:
    """The admitted and waiting transactions of one event loop.

    A transaction is admitted for all its Vaults at once or not at all: it
    never holds one Vault's slot while it waits for another's. Waiters are
    served first come, first served, by capacity (``grant``): the first
    waiter that cannot be admitted yet on a Vault reserves one slot of it
    against the waiters behind it, which use the slots left. A later
    transaction therefore never takes the last slot of a Vault that an
    earlier waiter needs, so single-Vault transactions arriving one after
    another cannot keep a transaction of several Vaults waiting until its
    deadline; and waiters blocked on a full Vault hold at most one slot of
    each other Vault they name, so that Vault keeps admitting with the rest.
    """

    def __init__(self) -> None:
        self.admitted: dict[uuid.UUID, int] = {}
        self.waiters: list[_Waiter] = []

    def take(self, vault_ids: list[uuid.UUID]) -> None:
        for v in vault_ids:
            self.admitted[v] = self.admitted.get(v, 0) + 1

    def give_back(self, vault_ids: list[uuid.UUID], limit: int) -> None:
        for v in vault_ids:
            left = self.admitted[v] - 1
            if left:
                self.admitted[v] = left
            else:
                del self.admitted[v]
        self.grant(limit)

    def grant(self, limit: int) -> None:
        """Admit the waiters that fit, in arrival order.

        Called whenever a slot is given back or a waiter leaves, and for each
        new waiter. A waiter is admitted when each of its Vaults has a slot
        free beyond the one a waiter before it may have reserved there;
        otherwise it reserves a slot of each of its Vaults that no waiter
        before it has reserved, and the walk goes on.
        """
        reserved: set[uuid.UUID] = set()
        for waiter in list(self.waiters):
            if waiter.granted.done():
                self.waiters.remove(waiter)
            elif all(
                self.admitted.get(v, 0) + (1 if v in reserved else 0) < limit for v in waiter.vault_ids
            ):
                self.take(waiter.vault_ids)
                waiter.granted.set_result(None)
                self.waiters.remove(waiter)
            else:
                reserved.update(waiter.vault_ids)


# asyncio futures belong to their loop, so the state is kept per loop (tests
# run each in a loop of its own).
_states: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _State] = weakref.WeakKeyDictionary()
_warned_kinds: set[str] = set()


def _state() -> _State:
    loop = asyncio.get_running_loop()
    state = _states.get(loop)
    if state is None:
        state = _states[loop] = _State()
    return state


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


@asynccontextmanager
async def vault_write_admission(
    *vault_ids: uuid.UUID | str | None, vault: str | None = None, timeout: float | None = None,
) -> AsyncIterator[None]:
    """Admit the current task to write events for ``vault_ids``.

    Enter it before acquiring the transaction's pool connection and hold it
    until the transaction has ended. ``None`` ids are ignored, so a caller
    whose Vault does not exist proceeds to its own not-found handling.
    ``vault`` names the Vault in the error message. Raises ``WriteBusyError``
    when the Vaults do not all have a free slot within ``timeout``, by
    default ``vault_write_queue_timeout_secs``.
    """
    held = _admitted.get()
    wanted = sorted({_as_uuid(v) for v in vault_ids if v is not None} - held)
    if not wanted:
        yield
        return
    # Read when used: the outbox module imports this one, and must stay
    # importable where no configuration is loaded.
    from app.config import settings

    state = _state()
    limit = max(1, settings.vault_write_concurrency)
    if timeout is None:
        timeout = settings.vault_write_queue_timeout_secs
    waiter = _Waiter(wanted, asyncio.get_running_loop().create_future())
    state.waiters.append(waiter)
    state.grant(limit)
    if not waiter.granted.done():
        if len(state.waiters) > max(1, settings.vault_write_max_waiters):
            # The queue is full: refuse at once rather than wait, so that the
            # queue, and each walk of it, stays bounded. The new waiter is
            # last, so leaving changes nothing for the others.
            waiter.granted.cancel()
            state.waiters.remove(waiter)
            raise WriteBusyError(vault or ", ".join(str(v) for v in wanted), 0.0, retry_after_secs=1)
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(waiter.granted)
        except BaseException as exc:
            if waiter.granted.done():
                # Granted as the wait ended: give the slots back.
                state.give_back(wanted, limit)
            else:
                # Leave the queue, and admit the waiters its reservation
                # kept waiting.
                waiter.granted.cancel()
                state.waiters.remove(waiter)
                state.grant(limit)
            if isinstance(exc, TimeoutError):
                logger.warning(
                    "vault write admission timed out after %.1fs for %s",
                    time.monotonic() - started, vault or ", ".join(str(v) for v in wanted),
                )
                raise WriteBusyError(
                    vault or ", ".join(str(v) for v in wanted), timeout, retry_after_secs=1,
                ) from None
            raise
    token = _admitted.set(held | frozenset(wanted))
    try:
        yield
    finally:
        _admitted.reset(token)
        state.give_back(wanted, limit)


# How long giving a connection back to the pool may take, its reset included:
# an asyncpg release is given this bound, past which asyncpg closes the
# connection rather than reset it.
GIVE_BACK_GRACE_SECS = 0.5
# How much longer than the grace a release is awaited before the caller goes
# on without it.
_GIVE_BACK_MARGIN_SECS = 0.1

# Connections whose release ``give_back_connection`` has started and that has
# not ended; ``close_pooled_connection`` leaves them alone.
_releasing: set[int] = set()


def close_pooled_connection(conn: Any) -> bool:
    """Close a connection taken from a pool, before any release of it has
    started, so that the pool takes it back without a reset: the plan reader
    closes one whose statement it gave up on. False when there is nothing to
    close: a connection without ``terminate``, one already back in the pool,
    which asyncpg's pool proxy refuses to touch once it is released, or one
    whose release has started. A connection must not be closed under its
    release: asyncpg's release closes, when its bound expires, whatever
    connection the pool's holder has then, and once a close had given the
    holder back to the pool, that would be another request's."""
    if id(conn) in _releasing:
        return False
    terminate = getattr(conn, "terminate", None)
    if terminate is None:
        return False
    try:
        terminate()
    except asyncpg.InterfaceError:
        return False
    return True


def _start_release(acquire: Any, exc_info: tuple, grace: float) -> tuple[asyncio.Future, bool]:
    """Start giving back the connection taken with ``acquire``; and say
    whether the release is bounded by the grace.

    An acquisition that names its pool and connection, as asyncpg's does, is
    released with the pool's own bound, ``pool.release(conn, timeout=grace)``:
    a reset, or a wait for a cancelled statement's cancellation to end, that
    runs past the grace has asyncpg close the connection and raise
    ``TimeoutError``, and the pool gets the connection's place back once,
    when that release ends. Another context is left as ``async with`` leaves
    it.
    """
    release = getattr(getattr(acquire, "pool", None), "release", None)
    conn = getattr(acquire, "connection", None)
    if conn is not None and callable(release):
        acquire.connection, acquire.done = None, True
        return asyncio.ensure_future(release(conn, timeout=grace)), True
    return asyncio.ensure_future(acquire.__aexit__(*exc_info)), False


def _bound_expired(release: asyncio.Future, bounded: bool) -> bool:
    """Whether a finished release ended at the bound it was given. asyncpg's
    bound is asyncio's ``timeout()`` and ``wait_for()``, whose
    ``TimeoutError`` carries no ``errno``; a ``TimeoutError`` from the
    operating system, such as a socket's ``ETIMEDOUT``, carries one and is
    an error of the release like any other."""
    if not bounded or release.cancelled():
        return False
    error = release.exception()
    return isinstance(error, TimeoutError) and error.errno is None


def _release_error(release: asyncio.Future, bounded: bool) -> BaseException | None:
    """Retrieve a finished release's error, if any. The expiry of the bound
    it was given (``_bound_expired``) is not an error: asyncpg closed the
    connection, and the work on it was done."""
    if release.cancelled() or _bound_expired(release, bounded):
        return None
    return release.exception()


def _log_release_error(error: BaseException, situation: str) -> None:
    logger.warning("Giving a pool connection back failed %s", situation, exc_info=error)


def _retrieve(release: asyncio.Future, *, bounded: bool) -> None:
    """For a release that was left to the pool, still running: retrieve its
    error once it ends, and log it, since no caller is waiting for it."""
    error = _release_error(release, bounded)
    if error is not None:
        _log_release_error(error, "after its caller had gone on")


async def give_back_connection(
    acquire: Any, conn: Any, exc_info: tuple = (None, None, None), *, grace: float | None = None,
) -> bool:
    """Give ``conn``, taken with ``acquire`` (a ``pool.acquire()`` context),
    back to the pool, and return once the pool has it back.

    The release runs shielded from cancellation; a cancellation meanwhile is
    held back and raised once the wait ends. An asyncpg release carries
    asyncpg's own bound (``_start_release``): past the grace
    (``GIVE_BACK_GRACE_SECS`` by default) asyncpg closes the connection, and
    the pool gets its place back when that release ends. The connection is
    never closed here while its release runs (``close_pooled_connection``).
    A release that has not ended a little after the grace, which only
    asyncpg's own paths, or a context without that bound, can cause, is not
    awaited further: it is left to the pool, to finish in the background,
    with a warning, and the caller goes on. That is the one case where an
    admission can come back before the pool has the connection's place. A
    release that fails on its own raises its error, as leaving ``async with
    pool.acquire()`` does, except for the expiry of the bound it was given
    (``_bound_expired``): asyncpg closed the connection, and the work on it
    was done.
    A cancellation takes precedence over a release's error, which is then
    logged; so is the error of a release left to the pool. Returns whether
    the connection was not given back reset.
    """
    loop = asyncio.get_running_loop()
    grace = GIVE_BACK_GRACE_SECS if grace is None else grace
    key = id(conn)
    _releasing.add(key)
    release, bounded = _start_release(acquire, exc_info, grace)
    release.add_done_callback(lambda _release: _releasing.discard(key))
    deadline = loop.time() + grace + _GIVE_BACK_MARGIN_SECS
    cancelled: asyncio.CancelledError | None = None
    while not release.done():
        left = deadline - loop.time()
        if left <= 0:
            logger.warning(
                "A pool connection's release had not ended %.1fs after it started; going on "
                "without it and leaving the connection to the pool",
                grace + _GIVE_BACK_MARGIN_SECS,
            )
            release.add_done_callback(functools.partial(_retrieve, bounded=bounded))
            break
        try:
            await asyncio.wait({release}, timeout=left)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    if not release.done():
        if cancelled is not None:
            raise cancelled
        return True
    # Retrieved first, whatever ends the wait: a cancellation takes
    # precedence, and an error it keeps from the caller is logged instead.
    timed_out = _bound_expired(release, bounded)
    error = _release_error(release, bounded)
    if cancelled is not None:
        if error is not None:
            _log_release_error(error, "while its request was being cancelled")
        raise cancelled
    if error is not None:
        raise error
    return timed_out


@asynccontextmanager
async def _given_back(pool: Any) -> AsyncIterator[Any]:
    """A connection from ``pool`` that goes back through
    ``give_back_connection`` when the block ends, cancellation included."""
    acquire = pool.acquire()
    conn = await acquire.__aenter__()
    try:
        yield conn
    except BaseException:
        await give_back_connection(acquire, conn, sys.exc_info())
        raise
    await give_back_connection(acquire, conn)


@asynccontextmanager
async def admitted_connection(
    pool: Any, *vault_ids: uuid.UUID | str | None, vault: str | None = None,
    timeout: float | None = None,
) -> AsyncIterator[Any]:
    """Admit the current task for ``vault_ids`` (``vault_write_admission``),
    then take a connection from ``pool``.

    On the way out, cancellation included, the connection goes back to the
    pool first (``give_back_connection``) and the admission after it, so a
    Vault never has more of the pool's connections in use, being reset or
    not, than its admitted transactions. Every transaction that is admitted
    and then takes a pool connection goes through here.
    """
    async with vault_write_admission(*vault_ids, vault=vault, timeout=timeout):
        async with _given_back(pool) as conn:
            yield conn


@asynccontextmanager
async def acquire_given_back(pool: Any) -> AsyncIterator[Any]:
    """Take a connection from ``pool`` in code that can run inside an
    admission entered without a connection: a Native mutation admitted
    before it prepares its payload, Vault creation, and what they call.

    When the current task holds an admission, the connection goes back
    through ``give_back_connection``, so it is back in the pool before the
    admission is given back, cancellation included. Elsewhere this is
    ``pool.acquire()`` itself.
    """
    if not _admitted.get():
        async with pool.acquire() as conn:
            yield conn
        return
    async with _given_back(pool) as conn:
        yield conn


def is_admitted(vault_id: uuid.UUID | str | None) -> bool:
    """True when the current task is admitted to write events for the Vault."""
    return vault_id is not None and _as_uuid(vault_id) in _admitted.get()


def note_event_write(vault_id: uuid.UUID | str | None, kind: str) -> None:
    """Log, once per kind, an event written for a Vault without admission."""
    if vault_id is None or is_admitted(vault_id) or kind in _warned_kinds:
        return
    _warned_kinds.add(kind)
    logger.warning(
        "event %s written for vault %s without vault write admission; "
        "its path does not hold back other writers of the Vault before the pool",
        kind, vault_id,
    )


def snapshot() -> dict:
    """Admitted and waiting transactions of the running loop."""
    try:
        state = _state()
    except RuntimeError:
        return {"vaults": 0, "admitted": 0, "waiting": 0}
    return {
        "vaults": len(state.admitted),
        "admitted": sum(state.admitted.values()),
        "waiting": sum(1 for w in state.waiters if not w.granted.done()),
    }


class VaultReplacedError(Exception):
    """The Vault a name resolved to before admission is not the one the
    transaction found under that name: the Vault was deleted and another
    created with the name while the request waited."""


def check_admitted_vault(admitted: uuid.UUID | str | None, found: uuid.UUID | str | None) -> None:
    """Raise ``VaultReplacedError`` unless the Vault a transaction found by
    name is the one it was admitted for. A Vault found where none was
    admitted, or the other way round, is a replacement too."""
    if found is None and admitted is None:
        return
    if found is None or admitted is None or _as_uuid(found) != _as_uuid(admitted):
        raise VaultReplacedError


def retry_if_vault_replaced(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Run a write once more when its Vault was replaced while it waited.

    The first attempt has released its connection and its admission when the
    error reaches here, so the retry resolves the name afresh and is admitted
    for the Vault it finds. A second replacement is a ``ConflictError``.
    """
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except VaultReplacedError:
            pass
        try:
            return await fn(*args, **kwargs)
        except VaultReplacedError as exc:
            raise ConflictError("The Vault changed while this request waited; retry it") from exc

    return wrapper
