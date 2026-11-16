"""Unit tests for the per-process Vault write admission.

No database: these check the gate itself. That transactions of one Vault
beyond the limit wait without blocking another Vault, that a wait past the
deadline is a retryable `write_busy` and leaves nothing behind, that admission
is re-entrant within a task, that several Vaults are admitted together, that
waiters are served first come, first served, by capacity, and that an event
written without admission is logged.
"""

from __future__ import annotations

import asyncio
import errno
import gc
import logging
import uuid

import asyncpg
import pytest

from app.config import settings
from app.exceptions import WriteBusyError
from app.services import vault_write_admission as admission
from app.util.errors import WRITE_BUSY, exception_envelope


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(settings, "vault_write_concurrency", 2)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)


async def _hold(vault_id, entered: asyncio.Event, release: asyncio.Event) -> None:
    async with admission.vault_write_admission(vault_id):
        entered.set()
        await release.wait()


async def test_a_vault_admits_up_to_its_limit_and_queues_the_rest():
    vault = uuid.uuid4()
    release = asyncio.Event()
    entered = [asyncio.Event() for _ in range(3)]
    holders = [asyncio.create_task(_hold(vault, e, release)) for e in entered]
    await asyncio.wait_for(asyncio.gather(entered[0].wait(), entered[1].wait()), timeout=5)
    await asyncio.sleep(0.05)
    assert not entered[2].is_set(), "a third transaction entered a Vault limited to two"
    release.set()
    await asyncio.wait_for(asyncio.gather(*holders), timeout=5)
    assert entered[2].is_set()
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_a_full_vault_does_not_hold_back_another_vault():
    busy, other = uuid.uuid4(), uuid.uuid4()
    release = asyncio.Event()
    entered = [asyncio.Event() for _ in range(2)]
    holders = [asyncio.create_task(_hold(busy, e, release)) for e in entered]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)), timeout=5)
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(other):
            assert admission.is_admitted(other)
            assert not admission.is_admitted(busy)
    release.set()
    await asyncio.gather(*holders)


async def test_a_wait_past_the_deadline_is_write_busy_and_leaves_nothing(monkeypatch):
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.1)
    vault = uuid.uuid4()
    release = asyncio.Event()
    entered = [asyncio.Event() for _ in range(2)]
    holders = [asyncio.create_task(_hold(vault, e, release)) for e in entered]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)), timeout=5)
    with pytest.raises(WriteBusyError) as busy:
        async with admission.vault_write_admission(vault, vault="busy-vault"):
            pytest.fail("admitted past the limit")
    assert busy.value.status_code == 429
    assert busy.value.retry_after_secs == 1
    assert "busy-vault" in str(busy.value)
    envelope = exception_envelope(busy.value)
    assert (envelope["code"], envelope["details"]["retry_after_secs"]) == (WRITE_BUSY, 1)
    assert not admission.is_admitted(vault)
    # Only the two holders remain; the timed-out waiter left nothing behind.
    assert admission.snapshot() == {"vaults": 1, "admitted": 2, "waiting": 0}
    release.set()
    await asyncio.gather(*holders)
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_admission_is_reentrant_within_a_task(monkeypatch):
    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    vault = uuid.uuid4()
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(vault):
            async with admission.vault_write_admission(vault):
                assert admission.is_admitted(vault)
            assert admission.is_admitted(vault)
    assert not admission.is_admitted(vault)


async def test_several_vaults_are_admitted_together_or_not_at_all(monkeypatch):
    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.1)
    first, second = sorted([uuid.uuid4(), uuid.uuid4()])
    release = asyncio.Event()
    entered = asyncio.Event()
    holder = asyncio.create_task(_hold(second, entered, release))
    await asyncio.wait_for(entered.wait(), timeout=5)
    with pytest.raises(WriteBusyError):
        async with admission.vault_write_admission(second, first, None):
            pytest.fail("admitted while one Vault was full")
    # It held no slot of `first` while it waited.
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(first):
            pass
    release.set()
    await holder
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(first, second):
            assert admission.is_admitted(first) and admission.is_admitted(second)


async def test_a_full_vault_does_not_delay_another_vaults_writes(monkeypatch):
    """Limit 2. A transaction for A and B waits while B is full. It holds no
    slot of A meanwhile and reserves one: a transaction for A alone is
    admitted at once to the other, and the A-and-B one follows when B
    frees."""
    a, b = uuid.uuid4(), uuid.uuid4()
    release_b = asyncio.Event()
    b_held = [asyncio.Event() for _ in range(2)]
    holders = [asyncio.create_task(_hold(b, e, release_b)) for e in b_held]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in b_held)), timeout=5)
    both_entered, release_both = asyncio.Event(), asyncio.Event()

    async def both() -> None:
        async with admission.vault_write_admission(a, b):
            both_entered.set()
            await release_both.wait()

    waiting = asyncio.create_task(both())
    await asyncio.sleep(0.05)
    assert admission.snapshot()["waiting"] == 1
    async with asyncio.timeout(0.5):
        async with admission.vault_write_admission(a):
            assert not both_entered.is_set()
    release_b.set()
    await asyncio.gather(*holders)
    await asyncio.wait_for(both_entered.wait(), timeout=5)
    release_both.set()
    await waiting
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_a_cancelled_waiter_leaves_no_slot_taken(monkeypatch):
    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    vault = uuid.uuid4()
    release = asyncio.Event()
    entered = asyncio.Event()
    holder = asyncio.create_task(_hold(vault, entered, release))
    await asyncio.wait_for(entered.wait(), timeout=5)
    waiter = asyncio.create_task(_hold(vault, asyncio.Event(), asyncio.Event()))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await holder
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(vault):
            pass


async def test_no_vault_is_no_admission():
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(None):
            assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_an_event_written_without_admission_is_logged_once(caplog, monkeypatch):
    monkeypatch.setattr(admission, "_warned_kinds", set())
    vault = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger="akb.vault_write_admission"):
        admission.note_event_write(vault, "document.put")
        admission.note_event_write(vault, "document.put")
        admission.note_event_write(None, "auth.signin")
        async with admission.vault_write_admission(vault):
            admission.note_event_write(vault, "table.create")
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 and "document.put" in messages[0], messages


async def test_a_transaction_of_two_vaults_is_not_starved_by_single_vault_ones(monkeypatch):
    """Transactions of Vault A alone and of Vault B alone keep arriving, one
    slot each. A transaction of both waits: when each freed slot went to the
    next single-Vault transaction, it never found both free before its
    deadline. It reserves A and B against the transactions behind it, so it
    is admitted once the ones before it are done; Vault C keeps flowing
    meanwhile."""
    import time

    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.6)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stop = asyncio.Event()
    others: list[float] = []

    async def stream(vault) -> None:
        while not stop.is_set():
            try:
                async with admission.vault_write_admission(vault):
                    await asyncio.sleep(0.05)
            except WriteBusyError:
                pass
            await asyncio.sleep(0)

    async def unrelated() -> None:
        while not stop.is_set():
            started = time.monotonic()
            async with admission.vault_write_admission(c):
                others.append(time.monotonic() - started)
                await asyncio.sleep(0.02)

    traffic = [asyncio.create_task(stream(v)) for v in (a, b) for _ in range(2)]
    traffic.append(asyncio.create_task(unrelated()))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    try:
        async with admission.vault_write_admission(a, b):
            waited = time.monotonic() - started
    finally:
        stop.set()
        await asyncio.gather(*traffic, return_exceptions=True)
    assert waited < 0.6, waited
    assert others and max(others) < 0.3, others


async def _wait_out(task: asyncio.Task, ending: str) -> None:
    """End a waiting task: let it reach its deadline, or cancel it."""
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(WriteBusyError):
            await task


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_a_waiter_leaving_admits_the_ones_its_reservation_held_back(monkeypatch, ending):
    """Limit 1. A transaction holds A, and B is free. W, for A and B, waits
    for A and reserves B; Q, for B alone, arrives after it, past half W's
    deadline, and waits behind it. When W gives up at its deadline, or is
    cancelled, Q is admitted at once, B being free all along. A waiter that
    left the queue used to wake nobody, and Q waited until its own
    deadline."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    a, b = uuid.uuid4(), uuid.uuid4()
    release, held = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(a, held, release))
    await asyncio.wait_for(held.wait(), timeout=5)

    async def both() -> None:
        async with admission.vault_write_admission(a, b, timeout=0.6):
            pytest.fail("admitted while A was full")

    waiting = asyncio.create_task(both())
    await asyncio.sleep(0.35)
    entered = asyncio.Event()

    async def only_b() -> None:
        async with admission.vault_write_admission(b, timeout=5.0):
            entered.set()

    queued = asyncio.create_task(only_b())
    await asyncio.sleep(0.05)
    assert not entered.is_set(), "a later transaction took a slot an earlier waiter reserved"
    await _wait_out(waiting, ending)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await queued
    release.set()
    await holder
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_a_waiter_reserves_the_last_slot_until_it_leaves(monkeypatch, ending):
    """Limit 4. A has three transactions and B four. W, for A and B, waits
    for B and reserves A's last slot, so Q, for A alone, waits behind it.
    When W leaves the queue, at its deadline or cancelled, Q is admitted at
    once to that slot."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 4)
    a, b = uuid.uuid4(), uuid.uuid4()
    release = asyncio.Event()
    held = [asyncio.Event() for _ in range(7)]
    holders = [
        asyncio.create_task(_hold(vault, e, release))
        for vault, e in zip([a] * 3 + [b] * 4, held)
    ]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in held)), timeout=5)

    async def both() -> None:
        async with admission.vault_write_admission(a, b, timeout=0.6):
            pytest.fail("admitted while B was full")

    waiting = asyncio.create_task(both())
    await asyncio.sleep(0.35)
    entered = asyncio.Event()

    async def only_a() -> None:
        async with admission.vault_write_admission(a, timeout=5.0):
            entered.set()

    queued = asyncio.create_task(only_a())
    await asyncio.sleep(0.05)
    assert not entered.is_set(), "a later transaction took the slot an earlier waiter reserved"
    await _wait_out(waiting, ending)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await queued
    release.set()
    await asyncio.gather(*holders)
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_a_waiter_reserves_only_the_slots_it_needs(monkeypatch):
    """Limit 4. B's four slots are taken, and A's are free. W, for A and B,
    waits for B and reserves one slot of A: three transactions for A alone
    are admitted at once, past half W's deadline, and a fourth waits behind
    W. W is admitted when B frees, and then the fourth. A waiter past half
    its deadline used to keep every free slot of its Vaults, and the
    transactions for A waited although none of A's slots was in use."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 4)
    a, b = uuid.uuid4(), uuid.uuid4()
    release_b, release_a = asyncio.Event(), asyncio.Event()
    b_held = [asyncio.Event() for _ in range(4)]
    b_holders = [asyncio.create_task(_hold(b, e, release_b)) for e in b_held]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in b_held)), timeout=5)
    both_entered = asyncio.Event()

    async def both() -> None:
        async with admission.vault_write_admission(a, b, timeout=1.0):
            both_entered.set()

    waiting = asyncio.create_task(both())
    await asyncio.sleep(0.6)
    a_held = [asyncio.Event() for _ in range(3)]
    a_holders = [asyncio.create_task(_hold(a, e, release_a)) for e in a_held]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in a_held)), timeout=0.2)
    fourth_entered = asyncio.Event()
    fourth = asyncio.create_task(_hold(a, fourth_entered, release_a))
    await asyncio.sleep(0.05)
    assert not fourth_entered.is_set(), "a later transaction took the slot W reserved"
    release_b.set()
    await asyncio.gather(*b_holders)
    await asyncio.wait_for(both_entered.wait(), timeout=1.0)
    await waiting
    await asyncio.wait_for(fourth_entered.wait(), timeout=1.0)
    release_a.set()
    await asyncio.gather(*a_holders, fourth)
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_waiters_blocked_on_a_full_vault_reserve_one_slot_of_another(monkeypatch):
    """Limit 4. A's four slots are taken, and four transactions for A and B
    wait for A. Only the first reserves a slot of B, so a transaction for B
    alone, waiting as long as a rollout does, is admitted at once to one of
    the three left. Each of the four waiters used to reserve a slot of B,
    all four together, and the transaction for B waited for a Vault none of
    them was using. When A frees a slot, the first waiter for A and B is
    admitted."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 4)
    a, b = uuid.uuid4(), uuid.uuid4()
    release_a = asyncio.Event()
    a_held = [asyncio.Event() for _ in range(4)]
    a_holders = [asyncio.create_task(_hold(a, e, release_a)) for e in a_held]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in a_held)), timeout=5)
    order: list[int] = []

    async def both(number: int) -> None:
        async with admission.vault_write_admission(a, b, timeout=5.0):
            order.append(number)

    waiting = []
    for number in range(4):
        waiting.append(asyncio.create_task(both(number)))
        await asyncio.sleep(0.01)
    async with asyncio.timeout(0.2):
        async with admission.vault_write_admission(b, timeout=settings.app_rollout_admission_wait_secs):
            assert admission.is_admitted(b)
    assert order == []
    release_a.set()
    await asyncio.gather(*a_holders, *waiting)
    assert order == [0, 1, 2, 3], order
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_a_request_finding_the_queue_full_is_refused_at_once(monkeypatch):
    """At most `vault_write_max_waiters` transactions wait. A holds its only
    slot and three transactions wait for it; a fourth, and one for another
    full Vault, are refused at once with a retryable write_busy, holding
    nothing, and the three waiting are admitted in turn when A frees. The
    queue used to grow without bound, and every arrival and departure walked
    all of it."""
    import time

    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_max_waiters", 3)
    a, other = uuid.uuid4(), uuid.uuid4()
    release = asyncio.Event()
    held = [asyncio.Event(), asyncio.Event()]
    holders = [asyncio.create_task(_hold(vault, e, release)) for vault, e in zip((a, other), held)]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in held)), timeout=5)
    entered = [asyncio.Event() for _ in range(3)]
    waiters = [asyncio.create_task(_hold(a, e, asyncio.Event())) for e in entered]
    await asyncio.sleep(0.05)
    assert admission.snapshot()["waiting"] == 3
    for vault in (a, other):
        started = time.monotonic()
        with pytest.raises(WriteBusyError) as refused:
            async with admission.vault_write_admission(vault):
                pytest.fail("admitted past a full queue")
        assert time.monotonic() - started < 0.1
        assert refused.value.retry_after_secs == 1
    assert admission.snapshot()["waiting"] == 3
    # A free slot is still taken at once, whatever waits elsewhere.
    async with asyncio.timeout(0.5):
        async with admission.vault_write_admission(uuid.uuid4()):
            pass
    for waiter in waiters:
        waiter.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)
    release.set()
    await asyncio.gather(*holders)
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


class _ResettingPool:
    """A pool of ``size`` connections released the way asyncpg's are.

    ``release(conn, timeout=...)`` runs shielded from the caller's
    cancellation and resets the connection for ``reset_secs``, or until it is
    closed; past ``timeout`` it closes the connection, gives its place back
    and raises ``TimeoutError``, as asyncpg's bound does. Leaving ``async
    with pool.acquire()`` releases without a bound. ``in_use`` counts the
    connections taken and not yet back, being reset or not; a reader's
    acquisition (``reader=True``) is released without a reset. With ``hang``
    a release does not end, its bound notwithstanding, until ``unhang`` is
    set, and then gives the place back. With ``fail_with`` a release that
    ends within its bound gives the place back and raises that error."""

    def __init__(
        self, size: int, reset_secs: float, *, hang: bool = False, fail_with: BaseException | None = None,
    ) -> None:
        self._free = asyncio.Semaphore(size)
        self.reset_secs = reset_secs
        self.hang = hang
        self.fail_with = fail_with
        self.unhang = asyncio.Event()
        self.in_use = 0
        self.peak = 0
        self.resetting = asyncio.Event()

    def give_back_place(self) -> None:
        self.in_use -= 1
        self._free.release()

    def acquire(self, *, reader: bool = False) -> "_ResettingAcquire":
        return _ResettingAcquire(self, reader)

    async def release(self, conn: "_PooledProxy", *, timeout: float | None = None) -> None:
        await asyncio.shield(conn.acquired._release(timeout))


class _PooledProxy:
    def __init__(self, acquired: "_ResettingAcquire") -> None:
        self.acquired = acquired
        self.released = False
        self.closed = asyncio.Event()

    def terminate(self) -> None:
        if self.released:
            raise asyncpg.InterfaceError(
                "cannot call Connection.terminate(): connection has been released back to the pool",
            )
        self.closed.set()


class _ResettingAcquire:
    def __init__(self, pool: _ResettingPool, reader: bool) -> None:
        self.pool = pool
        self.connection: _PooledProxy | None = None
        self.done = False
        self._reader = reader
        self._proxy = _PooledProxy(self)

    async def __aenter__(self) -> _PooledProxy:
        await self.pool._free.acquire()
        self.pool.in_use += 1
        self.pool.peak = max(self.pool.peak, self.pool.in_use)
        self.connection = self._proxy
        return self._proxy

    async def __aexit__(self, *_exc) -> None:
        conn, self.connection, self.done = self.connection, None, True
        assert conn is not None
        await self.pool.release(conn)

    async def _release(self, timeout: float | None) -> None:
        if not self._reader:
            self.pool.resetting.set()
            if self.pool.hang:
                await self.pool.unhang.wait()
            else:
                reset = self.pool.reset_secs
                closed = asyncio.ensure_future(self._proxy.closed.wait())
                await asyncio.wait({closed}, timeout=reset if timeout is None else min(reset, timeout))
                closed.cancel()
                if not self._proxy.closed.is_set() and timeout is not None and timeout < reset:
                    self._proxy.closed.set()
                    self._proxy.released = True
                    self.pool.give_back_place()
                    raise TimeoutError("timed out resetting the connection")
        self._proxy.released = True
        self.pool.give_back_place()
        if not self._reader and self.pool.fail_with is not None:
            raise self.pool.fail_with


async def _cancelled_while_resetting(pool: _ResettingPool, write) -> list[float]:
    """Send ``write()`` five times, each cancelled once its connection's reset
    starts, the next one sent at once, while a reader of another Vault takes
    connections. Returns the reader's waits."""
    stop = asyncio.Event()
    waits: list[float] = []
    loop = asyncio.get_running_loop()

    async def reader() -> None:
        while not stop.is_set():
            started = loop.time()
            try:
                async with asyncio.timeout(1.5):
                    async with pool.acquire(reader=True):
                        waits.append(loop.time() - started)
            except TimeoutError:
                waits.append(loop.time() - started)
                return
            await asyncio.sleep(0.02)

    reading = asyncio.create_task(reader())
    writes = []
    try:
        for _ in range(5):
            pool.resetting.clear()
            task = asyncio.create_task(write())
            writes.append(task)
            await asyncio.wait_for(pool.resetting.wait(), timeout=10)
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*writes, return_exceptions=True), timeout=20)
    finally:
        stop.set()
        await asyncio.gather(reading, return_exceptions=True)
    assert all(task.cancelled() for task in writes)
    return waits


async def test_a_cancelled_write_gives_its_admission_back_once_its_connection_is_back(monkeypatch):
    """Limit 1, a pool of three connections whose reset takes two seconds.
    A write of Vault A is cancelled while its connection is being reset, five
    times in a row, the next one sent at once. Each keeps A's admission until
    its connection is back, which the release's own bound closes at the
    half-second grace, so A never has more than one connection in use, being
    reset or not, and a reader of Vault B keeps getting a connection. The
    admission used to be given back as soon as the cancelled release left
    the reset running, the next write of A took another connection, and the
    resets filled the pool."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 30.0)
    pool = _ResettingPool(size=3, reset_secs=2.0)
    a = uuid.uuid4()

    async def write() -> None:
        async with admission.admitted_connection(pool, a):
            pass

    waits = await _cancelled_while_resetting(pool, write)
    assert pool.peak <= 2, pool.peak
    assert waits and max(waits) < 1.0, max(waits)
    assert pool.in_use == 0
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_a_connection_taken_inside_an_admission_is_back_before_the_admission(monkeypatch):
    """The same pool and limit. A write of Vault A is admitted without a
    connection and then takes one with ``acquire_given_back``, as a Native
    mutation does to prepare its payload; it is cancelled while that
    connection is being reset, five times in a row, the next one sent at
    once. Each keeps A's admission until the connection is back, so A never
    has more than one connection in use and a reader of Vault B keeps getting
    one. Taken with ``pool.acquire()``, the connection was still being reset
    when the admission went back."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 30.0)
    pool = _ResettingPool(size=3, reset_secs=2.0)
    a = uuid.uuid4()

    async def write() -> None:
        async with admission.vault_write_admission(a):
            async with admission.acquire_given_back(pool):
                pass

    waits = await _cancelled_while_resetting(pool, write)
    assert pool.peak <= 2, pool.peak
    assert waits and max(waits) < 1.0, max(waits)
    assert pool.in_use == 0
    assert admission.snapshot() == {"vaults": 0, "admitted": 0, "waiting": 0}


async def test_outside_an_admission_acquire_given_back_is_the_pools_own():
    """With no admission to give back, ``acquire_given_back`` is
    ``pool.acquire()``: a task cancelled while its connection is being reset
    ends at once, and the reset goes on, its connection neither closed nor
    back yet."""
    pool = _ResettingPool(size=3, reset_secs=0.3)
    taken: list[_PooledProxy] = []

    async def read() -> None:
        async with admission.acquire_given_back(pool) as conn:
            taken.append(conn)

    task = asyncio.create_task(read())
    await asyncio.wait_for(pool.resetting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.in_use == 1
    assert not taken[0].closed.is_set()
    async with asyncio.timeout(5):
        while pool.in_use:
            await asyncio.sleep(0.02)
    assert not taken[0].closed.is_set()


async def test_a_release_that_does_not_end_is_left_to_the_pool_without_closing_its_connection(
    monkeypatch, caplog,
):
    """Limit 1, a pool whose release of a write's connection does not end
    within its bound, as only asyncpg's own paths could. A write of Vault A
    ends, and its release is awaited until a little after the grace and then
    left to finish in the background, with a warning, so the next write of
    A is admitted then; the connection is not closed under its release, and
    its place stays taken until the release ends. This is the one case where
    the admission comes back before the pool has the place. The write used
    to wait for such a release forever and keep A's admission."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 30.0)
    monkeypatch.setattr(admission, "GIVE_BACK_GRACE_SECS", 0.2)
    pool = _ResettingPool(size=2, reset_secs=60.0, hang=True)
    a = uuid.uuid4()
    loop = asyncio.get_running_loop()
    admitted_at: list[float] = []
    taken: list[_PooledProxy] = []

    async def write() -> None:
        async with admission.admitted_connection(pool, a) as conn:
            admitted_at.append(loop.time())
            taken.append(conn)

    caplog.set_level(logging.WARNING, logger="akb.vault_write_admission")
    writes: list[asyncio.Task] = []
    try:
        writes.append(asyncio.create_task(write()))
        await asyncio.wait_for(pool.resetting.wait(), timeout=5)
        pool.resetting.clear()
        # A connection under release is not closed, whoever asks.
        assert admission.close_pooled_connection(taken[0]) is False
        writes.append(asyncio.create_task(write()))
        # Not awaited directly: a cancelled await would cancel the write.
        done, _ = await asyncio.wait({writes[0]}, timeout=5)
        assert writes[0] in done, "the first write still waits for its connection's release"
        await asyncio.wait_for(pool.resetting.wait(), timeout=5)
        assert admitted_at[1] - admitted_at[0] < 0.2 + 0.1 + 0.3, admitted_at
        assert any("going on without it" in r.getMessage() for r in caplog.records)
        assert not taken[0].closed.is_set()
        assert pool.in_use == 2
        pool.unhang.set()
        async with asyncio.timeout(5):
            while pool.in_use:
                await asyncio.sleep(0.01)
        assert not taken[0].closed.is_set()
    finally:
        pool.unhang.set()
        for task in writes:
            task.cancel()
        await asyncio.gather(*writes, return_exceptions=True)


async def _cancelled_while_its_release_runs(pool: _ResettingPool) -> asyncio.Task:
    """A write of a Vault whose connection's release is under way when the
    write is cancelled; returns the ended write."""
    async def write() -> None:
        async with admission.admitted_connection(pool, uuid.uuid4()):
            pass

    writing = asyncio.create_task(write())
    await asyncio.wait_for(pool.resetting.wait(), timeout=5)
    writing.cancel()
    # Not awaited directly: a cancelled await would cancel the write.
    await asyncio.wait({writing}, timeout=5)
    return writing


async def test_a_cancelled_give_back_retrieves_the_releases_timeout(monkeypatch):
    """A write is cancelled while its connection's release resets the
    connection past the release's bound, which closes it and ends with
    `TimeoutError`. The write ends cancelled, and the release's
    `TimeoutError` is retrieved: nothing reports an exception that was never
    retrieved once the release is collected. The cancellation used to be
    raised before the release's exception was retrieved."""
    monkeypatch.setattr(admission, "GIVE_BACK_GRACE_SECS", 0.1)
    loop = asyncio.get_running_loop()
    reported: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        pool = _ResettingPool(size=2, reset_secs=5.0)
        writing = await _cancelled_while_its_release_runs(pool)
        assert writing.done() and writing.cancelled()
        del writing
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert not [c for c in reported if "never retrieved" in c.get("message", "")], reported
    assert pool.in_use == 0


async def test_a_cancelled_give_back_logs_the_releases_error(monkeypatch, caplog):
    """A write is cancelled while its connection's release runs, and the
    release then fails on its own, within its bound, with a connection
    error. The write ends cancelled, as its request asked, and the
    release's error, which the cancellation keeps from the caller, is
    logged with its traceback. It used to be neither raised nor retrieved."""
    failure = ConnectionResetError("the connection was lost while it was reset")
    pool = _ResettingPool(size=2, reset_secs=0.05, fail_with=failure)
    caplog.set_level(logging.WARNING, logger="akb.vault_write_admission")
    writing = await _cancelled_while_its_release_runs(pool)
    assert writing.done() and writing.cancelled()
    logged = [r for r in caplog.records if "while its request was being cancelled" in r.getMessage()]
    assert logged and logged[0].exc_info is not None and logged[0].exc_info[1] is failure, caplog.records


async def test_a_release_left_to_the_pool_logs_its_error_when_it_ends(monkeypatch, caplog):
    """A release that outlives the wait is left to the pool, and later fails
    with a connection error. Its error is retrieved and logged then, since
    no caller waits for it any more."""
    monkeypatch.setattr(admission, "GIVE_BACK_GRACE_SECS", 0.05)
    failure = ConnectionResetError("the connection was lost while it was reset")
    pool = _ResettingPool(size=2, reset_secs=5.0, hang=True, fail_with=failure)
    caplog.set_level(logging.WARNING, logger="akb.vault_write_admission")

    async def write() -> None:
        async with admission.admitted_connection(pool, uuid.uuid4()):
            pass

    await asyncio.wait_for(write(), timeout=5)
    assert any("going on without it" in r.getMessage() for r in caplog.records)
    pool.unhang.set()
    async with asyncio.timeout(5):
        while pool.in_use:
            await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    logged = [r for r in caplog.records if "after its caller had gone on" in r.getMessage()]
    assert logged and logged[0].exc_info is not None and logged[0].exc_info[1] is failure, caplog.records


def _socket_timeout() -> TimeoutError:
    return TimeoutError(errno.ETIMEDOUT, "Connection timed out")


async def test_a_cancelled_give_back_logs_a_socket_timeout_before_the_bound(caplog):
    """A write is cancelled while its connection's release runs, and the
    release then fails, before its bound, with a socket's `ETIMEDOUT`: an
    operating-system `TimeoutError`, which carries an errno. That is the
    release's error, not its bound's expiry, so it is logged with its
    traceback while the write ends cancelled. Every `TimeoutError` of a
    bounded release used to count as the bound's and went unreported."""
    failure = _socket_timeout()
    pool = _ResettingPool(size=2, reset_secs=0.05, fail_with=failure)
    caplog.set_level(logging.WARNING, logger="akb.vault_write_admission")
    writing = await _cancelled_while_its_release_runs(pool)
    assert writing.done() and writing.cancelled()
    logged = [r for r in caplog.records if "while its request was being cancelled" in r.getMessage()]
    assert logged and logged[0].exc_info is not None and logged[0].exc_info[1] is failure, caplog.records


async def test_a_socket_timeout_before_the_bound_reaches_the_writer():
    """The same release failure, on a write that is not cancelled: the
    write's admitted connection block raises it, as leaving `async with
    pool.acquire()` would. It used to be taken for the bound's expiry and
    dropped."""
    failure = _socket_timeout()
    pool = _ResettingPool(size=2, reset_secs=0.05, fail_with=failure)

    async def write() -> None:
        async with admission.admitted_connection(pool, uuid.uuid4()):
            pass

    with pytest.raises(TimeoutError) as raised:
        await asyncio.wait_for(write(), timeout=5)
    assert raised.value is failure and raised.value.errno == errno.ETIMEDOUT


async def test_a_write_cancelled_in_its_block_ends_cancelled_though_its_release_fails(caplog):
    """A write is cancelled inside its admitted connection block, before its
    connection goes back, and the release then fails, before its bound, with
    a socket's `ETIMEDOUT`. The cancellation, delivered by the block, still
    wins: the write ends cancelled, and the release's error is logged with
    its traceback. Only a cancellation arriving while the release ran used to
    win; this one gave way to the release's error."""
    failure = _socket_timeout()
    pool = _ResettingPool(size=2, reset_secs=0.05, fail_with=failure)
    caplog.set_level(logging.WARNING, logger="akb.vault_write_admission")
    inside = asyncio.Event()

    async def write() -> None:
        async with admission.admitted_connection(pool, uuid.uuid4()):
            inside.set()
            await asyncio.sleep(30)

    writing = asyncio.create_task(write())
    await asyncio.wait_for(inside.wait(), timeout=5)
    writing.cancel()
    # Not awaited directly: a cancelled await would cancel the write.
    await asyncio.wait({writing}, timeout=5)
    assert writing.done() and writing.cancelled()
    logged = [r for r in caplog.records if "while its request was being cancelled" in r.getMessage()]
    assert logged and logged[0].exc_info is not None and logged[0].exc_info[1] is failure, caplog.records
    assert pool.in_use == 0


async def test_a_timeout_well_before_the_bound_is_the_releases_error():
    """A release fails with a `TimeoutError` that carries no errno, as a
    reset statement's own `command_timeout` raises one, long before the
    release's bound. It came from inside the release, not from the bound, so
    the write raises it rather than taking it for the bound's expiry. Every
    errno-less `TimeoutError` used to count as the bound's."""
    failure = TimeoutError()
    pool = _ResettingPool(size=2, reset_secs=0.05, fail_with=failure)

    async def write() -> None:
        async with admission.admitted_connection(pool, uuid.uuid4()):
            pass

    with pytest.raises(TimeoutError) as raised:
        await asyncio.wait_for(write(), timeout=5)
    assert raised.value is failure
