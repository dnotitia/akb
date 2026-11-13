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
safe. The limit is per process: with several API processes a Vault can have
that many admitted transactions in each, and the event lock still orders them.

Admission is re-entrant within a task: a call nested in an admitted one for
the same Vault does not wait again. A transaction that writes events for
several Vaults is admitted for all of them at once, under one deadline, and
holds none of them while it waits, so a full Vault does not delay another
Vault's writes. Code inside an admitted transaction must not wait for another
admission, and must not take the Bare Git write lane, which is entered before
this admission. Vault creation takes the new Vault's write-lane gate (not a
global lane slot) and then its admission and holds both through its seed
document, whose write re-enters both.

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
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import ParamSpec, TypeVar

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
    never holds one Vault's slot while it waits for another's, so a full
    Vault does not delay the writes of another Vault that share a
    transaction with it. Waiters are granted in arrival order among those
    whose Vaults all have a free slot.
    """

    def __init__(self) -> None:
        self.admitted: dict[uuid.UUID, int] = {}
        self.waiters: list[_Waiter] = []

    def free(self, vault_ids: list[uuid.UUID], limit: int) -> bool:
        return all(self.admitted.get(v, 0) < limit for v in vault_ids)

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
        for waiter in list(self.waiters):
            if waiter.granted.done():
                self.waiters.remove(waiter)
            elif self.free(waiter.vault_ids, limit):
                self.take(waiter.vault_ids)
                waiter.granted.set_result(None)
                self.waiters.remove(waiter)


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
    *vault_ids: uuid.UUID | str | None, vault: str | None = None,
) -> AsyncIterator[None]:
    """Admit the current task to write events for ``vault_ids``.

    Enter it before acquiring the transaction's pool connection and hold it
    until the transaction has ended. ``None`` ids are ignored, so a caller
    whose Vault does not exist proceeds to its own not-found handling.
    ``vault`` names the Vault in the error message. Raises ``WriteBusyError``
    when the Vaults do not all have a free slot within
    ``vault_write_queue_timeout_secs``.
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
    timeout = settings.vault_write_queue_timeout_secs
    if state.free(wanted, limit):
        state.take(wanted)
    else:
        waiter = _Waiter(wanted, asyncio.get_running_loop().create_future())
        state.waiters.append(waiter)
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(waiter.granted)
        except BaseException as exc:
            if waiter.granted.done():
                # Granted as the wait ended: give the slots back.
                state.give_back(wanted, limit)
            else:
                waiter.granted.cancel()
                state.waiters.remove(waiter)
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
