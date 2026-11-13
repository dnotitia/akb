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
that hold no connection, in arrival order. A wait longer than
``vault_write_queue_timeout_secs`` raises ``WriteBusyError`` (HTTP 429 with
``Retry-After``, MCP ``write_busy``) before the request has done any database
or Git work, so retrying it is safe. The limit is per process: with several
API processes a Vault can have that many admitted transactions in each, and
the event lock still orders them.

Admission is re-entrant within a task: a call nested in an admitted one for
the same Vault does not wait again. A transaction that writes events for
several Vaults is admitted for all of them at once, in id order, under one
deadline. Code inside an admitted transaction must not wait for another
admission, and must not take the Bare Git write lane, which is entered before
this admission.

``emit_event`` and ``lock_vault_events`` log, once per event kind, an event
written for a Vault outside its admission; that names a path that skips it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from app.exceptions import WriteBusyError

logger = logging.getLogger("akb.vault_write_admission")

# Vaults the current task is admitted for.
_admitted: ContextVar[frozenset[uuid.UUID]] = ContextVar(
    "akb_vault_write_admitted", default=frozenset(),
)


class _Gate:
    """One Vault's admission slots in one event loop."""

    __slots__ = ("slots", "users")

    def __init__(self, limit: int) -> None:
        self.slots = asyncio.Semaphore(limit)
        # Admitted and waiting transactions; the gate is dropped at zero.
        self.users = 0


# asyncio primitives belong to the loop that first waits on them, so the gates
# are kept per loop (tests run each in a loop of its own).
_gates_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[uuid.UUID, _Gate]] = (
    weakref.WeakKeyDictionary()
)
_warned_kinds: set[str] = set()


def _gates() -> dict[uuid.UUID, _Gate]:
    loop = asyncio.get_running_loop()
    gates = _gates_by_loop.get(loop)
    if gates is None:
        gates = _gates_by_loop[loop] = {}
    return gates


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
    when no slot frees within ``vault_write_queue_timeout_secs``.
    """
    held = _admitted.get()
    wanted = sorted({_as_uuid(v) for v in vault_ids if v is not None} - held)
    if not wanted:
        yield
        return
    # Read when used: the outbox module imports this one, and must stay
    # importable where no configuration is loaded.
    from app.config import settings

    gates = _gates()
    limit = max(1, settings.vault_write_concurrency)
    timeout = settings.vault_write_queue_timeout_secs
    entered: list[tuple[uuid.UUID, _Gate]] = []
    acquired: list[_Gate] = []
    started = time.monotonic()
    try:
        for vault_id in wanted:
            gate = gates.get(vault_id)
            if gate is None:
                gate = gates[vault_id] = _Gate(limit)
            gate.users += 1
            entered.append((vault_id, gate))
        try:
            async with asyncio.timeout(timeout):
                for _, gate in entered:
                    await gate.slots.acquire()
                    acquired.append(gate)
        except TimeoutError:
            logger.warning(
                "vault write admission timed out after %.1fs for %s",
                time.monotonic() - started, vault or ", ".join(str(v) for v in wanted),
            )
            raise WriteBusyError(
                vault or ", ".join(str(v) for v in wanted), timeout, retry_after_secs=1,
            ) from None
        token = _admitted.set(held | frozenset(wanted))
        try:
            yield
        finally:
            _admitted.reset(token)
    finally:
        for gate in acquired:
            gate.slots.release()
        for vault_id, gate in entered:
            gate.users -= 1
            if gate.users == 0 and gates.get(vault_id) is gate:
                del gates[vault_id]


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
    """Admitted and waiting transactions per Vault in the running loop."""
    try:
        gates = _gates()
    except RuntimeError:
        return {"vaults": 0}
    return {"vaults": len(gates), "transactions": sum(g.users for g in gates.values())}
