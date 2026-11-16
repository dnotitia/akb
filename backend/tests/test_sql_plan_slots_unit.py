"""Raw SQL plans are read a few at a time, and one at a time per Vault.

A raw SQL write reads its plan on a pool connection before its admission. At
most `akb_sql_plan_concurrency` plans are read at once per process, and a plan
holds a token for every Vault its statement declares, taken all at once or
not at all, so at most one plan per Vault is read at a time. Waiters hold
nothing and are served first come, first served, by capacity: one that waits
reserves its Vaults' tokens against the statements behind it. The plans of a
Vault whose table is locked therefore wait one behind another, whatever other
Vaults they declare with it, and a statement of a Vault no earlier waiter
declares takes the free slot. A wait that ends without a slot leaves the
queue at once, and the statements its reservation held back go on.
"""

from __future__ import annotations

import asyncio
import errno
from typing import Any
import time

import asyncpg
import pytest

from app.config import settings
from app.db.transient import transient_error
from app.exceptions import WriteBusyError
from app.services import user_sql_executor
from app.util.errors import OPERATION_TIMEOUT


def _slots() -> user_sql_executor._PlanSlots:
    return user_sql_executor._plan_slots[asyncio.get_running_loop()]


async def _hold(vaults: list[str], holding: asyncio.Event, release: asyncio.Event) -> None:
    async with user_sql_executor._plan_slot(vaults):
        holding.set()
        await release.wait()


async def test_plans_of_one_vault_go_one_at_a_time_and_another_vault_takes_the_free_slot(monkeypatch):
    """Six plans of one Vault wait, then one of another Vault. The busy
    Vault's plans are read one at a time, so the other Vault's plan takes the
    second slot at once; never more than two plans are read together."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    order: list[str] = []
    reading: dict[str, int] = {}
    peaks = {"all": 0, "busy": 0}

    async def plan(vaults: list[str], label: str) -> None:
        async with user_sql_executor._plan_slot(vaults):
            for vault in vaults:
                reading[vault] = reading.get(vault, 0) + 1
            peaks["all"] = max(peaks["all"], sum(reading.values()))
            peaks["busy"] = max(peaks["busy"], reading.get("busy", 0))
            order.append(label)
            await asyncio.sleep(0.02)
            for vault in vaults:
                reading[vault] -= 1

    busy = [asyncio.create_task(plan(["busy"], f"busy-{i}")) for i in range(6)]
    await asyncio.sleep(0)
    other = asyncio.create_task(plan(["quiet"], "quiet"))
    await asyncio.gather(*busy, other)
    assert (peaks["all"], peaks["busy"]) == (2, 1)
    assert order.index("quiet") == 1, order
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_statement_waits_for_every_vault_it_declares_holding_none(monkeypatch):
    """`a` and `d` are busy. A statement declaring `c` and `d` waits for `d`;
    one declaring `a` and `c` waits for `a`, then one declaring only `c`.
    The waiters hold no token: when `d` is free, the first is read, though a
    later waiter declares `c` too. Each reserves its Vaults against those
    behind it, so the statement of `c` alone, free to start all along, is
    read after the one of `a` and `c` that came before it."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 3)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    order: list[str] = []
    holding_a, release_a = asyncio.Event(), asyncio.Event()
    holding_d, release_d = asyncio.Event(), asyncio.Event()
    holders = [
        asyncio.create_task(_hold(["a"], holding_a, release_a)),
        asyncio.create_task(_hold(["d"], holding_d, release_d)),
    ]
    await asyncio.gather(holding_a.wait(), holding_d.wait())

    async def plan(vaults: list[str], label: str, seconds: float) -> None:
        async with user_sql_executor._plan_slot(vaults):
            order.append(label)
            await asyncio.sleep(seconds)

    first = asyncio.create_task(plan(["c", "d"], "c+d", 0.05))
    await asyncio.sleep(0)
    both = asyncio.create_task(plan(["c", "a", "c"], "a+c", 0))
    await asyncio.sleep(0)
    only_c = asyncio.create_task(plan(["c"], "c", 0))
    await asyncio.sleep(0.01)
    assert order == [], order
    release_d.set()
    await asyncio.sleep(0.01)
    assert order == ["c+d"], order
    release_a.set()
    await asyncio.gather(*holders, first, both, only_c)
    assert order == ["c+d", "a+c", "c"], order
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_vault_behind_plans_of_a_busy_vault_with_many_companions_is_not_held_back(monkeypatch):
    """Vault `a`'s table is locked, so each of its plans takes 0.1 s. Sixty
    statements write it, each declaring `a` with a different companion
    Vault, and then one declares `b`. The `a` plans are read one at a time,
    whatever their companions, and `b`'s takes the other slot at once. With
    one queue per declared set, `b` waited behind a turn of each of the sixty
    sets."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 3.0)

    async def plan(vaults: list[str], seconds: float) -> None:
        async with user_sql_executor._plan_slot(vaults):
            await asyncio.sleep(seconds)

    busy = [asyncio.create_task(plan(["a", f"c{i}"], 0.1)) for i in range(60)]
    await asyncio.sleep(0)
    started = time.monotonic()
    await plan(["b"], 0)
    took = time.monotonic() - started
    for task in busy:
        task.cancel()
    await asyncio.gather(*busy, return_exceptions=True)
    assert took < 0.05, took
    assert (_slots().running, _slots().waiters) == (0, [])


async def test_plan_waits_that_time_out_leave_the_queue_at_once(monkeypatch):
    """Two hundred statements wait behind a plan that keeps its slot and its
    Vault, and give up at their deadline with write_busy. Each leaves the
    queue as it gives up, while the plan is still read: the queue does not
    keep what nobody waits for."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.05)
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(["busy"], holding, release))
    await holding.wait()

    async def plan(vault: str) -> None:
        async with user_sql_executor._plan_slot([vault]):
            pass

    outcomes = await asyncio.gather(
        *(plan("busy" if i % 2 else f"other-{i}") for i in range(200)), return_exceptions=True,
    )
    assert all(isinstance(o, WriteBusyError) and o.retry_after_secs == 1 for o in outcomes)
    assert _slots().waiters == []
    assert (_slots().running, _slots().busy) == (1, {"busy"})
    release.set()
    await holder
    async with asyncio.timeout(1.0):
        async with user_sql_executor._plan_slot(["busy"]):
            assert _slots().running == 1
    assert (_slots().running, _slots().busy) == (0, set())


async def test_a_cancelled_plan_wait_leaves_the_queue_at_once(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(["busy"], holding, release))
    await holding.wait()

    async def wait_for_a_slot() -> None:
        async with user_sql_executor._plan_slot(["quiet"]):
            pass

    waiter = asyncio.create_task(wait_for_a_slot())
    await asyncio.sleep(0.01)
    assert len(_slots().waiters) == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert _slots().waiters == []
    release.set()
    await holder
    assert (_slots().running, _slots().busy) == (0, set())
    async with asyncio.timeout(1.0):
        async with user_sql_executor._plan_slot(["quiet"]):
            assert _slots().running == 1


async def _single_vault_traffic(enter, vaults: list[str], stop: asyncio.Event) -> list[asyncio.Task]:
    """Requests for one Vault each, two at a time per Vault, one after
    another, each holding its Vault for 50 ms: a later request of a Vault is
    always waiting."""

    async def stream(vault) -> None:
        while not stop.is_set():
            try:
                async with enter([vault]):
                    await asyncio.sleep(0.05)
            except WriteBusyError:
                pass
            await asyncio.sleep(0)

    return [asyncio.create_task(stream(vault)) for vault in vaults for _ in range(2)]


async def test_a_plan_of_two_vaults_is_not_starved_by_single_vault_plans(monkeypatch):
    """Single-Vault plans of `a` and of `b` keep arriving. A statement
    declaring both waits: when each freed token went to the next single-Vault
    plan, it never found both free before its deadline. It reserves `a` and
    `b` against the plans behind it, so it is read once the ones before it
    are done; `c` keeps flowing meanwhile."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 3)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.6)
    stop = asyncio.Event()
    traffic = await _single_vault_traffic(user_sql_executor._plan_slot, ["a", "b"], stop)
    others: list[float] = []

    async def unrelated() -> None:
        while not stop.is_set():
            started = time.monotonic()
            async with user_sql_executor._plan_slot(["c"]):
                others.append(time.monotonic() - started)
                await asyncio.sleep(0.02)

    traffic.append(asyncio.create_task(unrelated()))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    try:
        async with user_sql_executor._plan_slot(["a", "b"]):
            waited = time.monotonic() - started
    finally:
        stop.set()
        await asyncio.gather(*traffic, return_exceptions=True)
    assert waited < 0.6, waited
    assert others and max(others) < 0.3, others


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_a_plan_wait_leaving_starts_the_ones_its_reservation_held_back(monkeypatch, ending):
    """`a` is busy and `b` free. W, declaring `a` and `b`, waits for `a` and
    reserves `b`; Q, declaring `b`, arrives after it, past half W's
    deadline, and waits behind it. When W gives up at its deadline, or is
    cancelled, Q is read at once. A waiter that left the queue used to wake
    nobody, and Q waited until its own deadline."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2)
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(["a"], holding, release))
    await holding.wait()

    async def both() -> None:
        async with user_sql_executor._plan_slot(["a", "b"]):
            pytest.fail("read while `a` was busy")

    # W's deadline is short, Q's long: the deadline is read when a wait starts.
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.6)
    waiting = asyncio.create_task(both())
    await asyncio.sleep(0.35)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    started = asyncio.Event()

    async def only_b() -> None:
        async with user_sql_executor._plan_slot(["b"]):
            started.set()

    queued = asyncio.create_task(only_b())
    await asyncio.sleep(0.05)
    assert not started.is_set(), "a later statement took a token an earlier waiter reserved"
    if ending == "cancel":
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    else:
        with pytest.raises(WriteBusyError):
            await waiting
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await queued
    release.set()
    await holder
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_waiting_plan_leaves_the_free_slots_to_other_vaults(monkeypatch):
    """Four plans at once. `b` is busy, and W, declaring `a` and `b`, waits
    for it, reserving `a` and `b` only: statements of `c`, `d` and `e` take
    the three free slots at once, and one of `a` waits behind W. W is read
    when `b` is free, and then the one of `a`."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 4)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    holding, release_b = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(["b"], holding, release_b))
    await holding.wait()
    order: list[str] = []

    async def plan(vaults: list[str], label: str) -> None:
        async with user_sql_executor._plan_slot(vaults):
            order.append(label)

    both = asyncio.create_task(plan(["a", "b"], "a+b"))
    await asyncio.sleep(0.01)
    release_others = asyncio.Event()
    others = [asyncio.Event() for _ in range(3)]
    other_tasks = [
        asyncio.create_task(_hold([vault], held, release_others))
        for vault, held in zip(["c", "d", "e"], others)
    ]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in others)), timeout=0.2)
    only_a = asyncio.create_task(plan(["a"], "a"))
    await asyncio.sleep(0.01)
    assert order == [], order
    release_others.set()
    await asyncio.gather(*other_tasks)
    await asyncio.sleep(0.01)
    assert order == [], order
    release_b.set()
    await asyncio.gather(holder, both, only_a)
    assert order == ["a+b", "a"], order
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_statement_finding_the_plan_queue_full_is_refused_at_once(monkeypatch):
    """At most `akb_sql_plan_max_waiters` statements wait for a plan slot.
    One plan holds the only slot and three statements wait; a fourth is
    refused at once with a retryable write_busy, and the three are read in
    turn once the slot is free."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    monkeypatch.setattr(settings, "akb_sql_plan_max_waiters", 3)
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(["busy"], holding, release))
    await holding.wait()
    read: list[str] = []

    async def plan(vault: str) -> None:
        async with user_sql_executor._plan_slot([vault]):
            read.append(vault)

    waiting = [asyncio.create_task(plan(f"w{i}")) for i in range(3)]
    await asyncio.sleep(0.01)
    assert len(_slots().waiters) == 3
    started = time.monotonic()
    with pytest.raises(WriteBusyError) as refused:
        await plan("late")
    assert time.monotonic() - started < 0.1
    assert refused.value.retry_after_secs == 1
    assert len(_slots().waiters) == 3
    release.set()
    await asyncio.gather(holder, *waiting)
    assert read == ["w0", "w1", "w2"]
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


class _Txn:
    """A transaction of the fake connection below; its start can be slow."""

    def __init__(self, conn: "_SlowConn") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_Txn":
        if self._conn.slow_start:
            await asyncio.sleep(self._conn.slow_start)
        return self

    async def __aexit__(self, exc_type, *_exc) -> bool:
        if exc_type is not None and self._conn.stuck_rollback:
            # A rollback that does not finish until the connection closes.
            await self._conn.terminated.wait()
            raise RuntimeError("connection is closed")
        if exc_type is not None and self._conn.slow_rollback:
            # A rollback that takes its time, unless the connection closes.
            closed = asyncio.ensure_future(self._conn.terminated.wait())
            await asyncio.wait({closed}, timeout=self._conn.slow_rollback)
            closed.cancel()
            if self._conn.terminated.is_set():
                raise RuntimeError("connection is closed")
        return False


class _SlowConn:
    """A connection whose statements take the time a test gives them: the
    caller's settings (``set_config('akb.actor_id', …)``) and ``EXPLAIN``
    for statements of a slow actor, and a transaction's start."""

    def __init__(
        self, *, settings_secs: float = 0.0, explain_secs: float = 0.0, slow_start: float = 0.0,
        stuck_rollback: bool = False, slow_rollback: float = 0.0,
    ) -> None:
        self.settings_secs = settings_secs
        self.explain_secs = explain_secs
        self.slow_start = slow_start
        self.stuck_rollback = stuck_rollback
        self.slow_rollback = slow_rollback
        self.terminated = asyncio.Event()
        self.terminated_at: float | None = None

    def transaction(self) -> _Txn:
        return _Txn(self)

    def terminate(self) -> None:
        if not self.terminated.is_set():
            self.terminated_at = asyncio.get_running_loop().time()
        self.terminated.set()

    async def execute(self, sql: str, *args) -> str:
        if "akb.actor_id" in sql and args and args[0] == "slow":
            await asyncio.sleep(self.settings_secs)
        return "OK"

    async def fetchval(self, sql: str, *args):
        if sql.startswith("EXPLAIN"):
            if "slow_table" in sql:
                await asyncio.sleep(self.explain_secs)
            return [{"Plan": {"Node Type": "Result"}}]
        return True  # a live human administrator

    async def fetch(self, sql: str, *args) -> list:
        return []


class _Acquire:
    def __init__(self, conn: _SlowConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _SlowConn:
        return self._conn

    async def __aexit__(self, *_exc) -> bool:
        return False


class _ConnPool:
    """Hands out the given connections in turn, then plain ones."""

    def __init__(self, *conns: _SlowConn) -> None:
        self._conns = list(conns)

    def acquire(self) -> _Acquire:
        return _Acquire(self._conns.pop(0) if self._conns else _SlowConn())


def _statement(vault: str, *, slow: bool) -> "user_sql_executor._Statement":
    return user_sql_executor._Statement(
        user_id="00000000-0000-0000-0000-000000000001", actor_id="slow" if slow else vault,
        sql=f"INSERT INTO {'slow_table' if slow else 'quick_table'} VALUES (1)", params=[],
        fetch=False, is_admin=True, vault_names=[vault], check_policy=False,
    )


async def _two_slow_plans_and_a_third(pool: _ConnPool) -> tuple[list, float]:
    """Plans of `x` and `y` take both slots; a plan of `z` waits behind
    them. Returns the outcomes of the first two and how long `z` took."""
    executor = user_sql_executor.UserSqlExecutor(pool)
    slow = [asyncio.create_task(executor._planned(_statement(v, slow=True))) for v in ("x", "y")]
    await asyncio.sleep(0.01)
    started = time.monotonic()
    assert await asyncio.wait_for(executor._planned(_statement("z", slow=False)), timeout=5) == frozenset()
    took = time.monotonic() - started
    outcomes = await asyncio.gather(*slow, return_exceptions=True)
    return outcomes, took


async def test_plan_statements_each_within_their_limit_cannot_pass_the_plan_deadline_together(monkeypatch):
    """Plans of `x` and `y` hold both plan slots. Each spends 0.3 s on its
    settings and 0.3 s on EXPLAIN, each within its own limit and together
    past the plan's 0.4 s deadline. Both fail with write_busy at the
    deadline and give their slots back, so the plan of `z` behind them is
    read soon after. The deadline used to bound only the wait for a
    connection, and each plan kept its slot for 0.6 s."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2)
    monkeypatch.setattr(settings, "akb_sql_plan_timeout_ms", 400)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    pool = _ConnPool(*(_SlowConn(settings_secs=0.3, explain_secs=0.3) for _ in range(2)))
    outcomes, took = await _two_slow_plans_and_a_third(pool)
    assert all(isinstance(o, WriteBusyError) for o in outcomes), outcomes
    assert took < 0.5, took
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_plan_whose_transaction_is_slow_to_start_gives_its_slot_back_at_the_deadline(monkeypatch):
    """Plans of `x` and `y` hold both plan slots, and their transactions
    take a second to start. They fail with write_busy at the plan's 0.2 s
    deadline, so the plan of `z` behind them is read soon after. A slow
    start used to be outside the deadline."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2)
    monkeypatch.setattr(settings, "akb_sql_plan_timeout_ms", 200)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    pool = _ConnPool(*(_SlowConn(slow_start=1.0) for _ in range(2)))
    outcomes, took = await _two_slow_plans_and_a_third(pool)
    assert all(isinstance(o, WriteBusyError) for o in outcomes), outcomes
    assert took < 0.5, took
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_plan_that_cannot_roll_back_in_time_closes_its_connection(monkeypatch):
    """A plan's EXPLAIN runs past the plan's 0.2 s deadline, and its
    rollback would not finish while the connection is open. The plan closes
    its connection at the deadline instead of waiting to roll back or give
    it back, and fails with write_busy; its slot is free again."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "akb_sql_plan_timeout_ms", 200)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    monkeypatch.setattr(user_sql_executor, "_PLAN_CLEANUP_SECS", 0.1)
    stuck = _SlowConn(explain_secs=5.0, stuck_rollback=True)
    executor = user_sql_executor.UserSqlExecutor(_ConnPool(stuck))
    plan = asyncio.create_task(executor._planned(_statement("x", slow=True)))
    done, _pending = await asyncio.wait({plan}, timeout=0.6)
    if not done:
        # Unblock the stuck read so that the failure does not hang.
        stuck.terminate()
        plan.cancel()
        await asyncio.gather(plan, return_exceptions=True)
        pytest.fail("the plan still held its connection 0.4 s past its deadline")
    assert stuck.terminated.is_set()
    with pytest.raises(WriteBusyError):
        plan.result()
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


class _PooledConnection:
    """asyncpg's pool connection proxy, as far as a plan sees it: statements
    of a plain fake, and a ``terminate`` that closes the connection under it
    until the connection is released back to the pool, and refuses after."""

    def __init__(self, raw: "_RawConnection") -> None:
        self._raw: "_RawConnection | None" = raw
        self.acquired: Any = None

    def _connection(self) -> "_RawConnection":
        if self._raw is None:
            raise asyncpg.InterfaceError(
                "cannot call Connection.terminate(): connection has been released back to the pool",
            )
        return self._raw

    def terminate(self) -> None:
        self._connection().terminate()

    def transaction(self) -> _Txn:
        return _Txn(_SlowConn())

    async def execute(self, sql: str, *args) -> str:
        return "OK"

    async def fetchval(self, sql: str, *args):
        if sql.startswith("EXPLAIN"):
            return [{"Plan": {"Node Type": "Result"}}]
        return True

    async def fetch(self, sql: str, *args) -> list:
        return []


class _RawConnection:
    def __init__(self) -> None:
        self.terminated = asyncio.Event()

    def terminate(self) -> None:
        self.terminated.set()


class _ReleasingPool:
    """A pool whose release resets the connection the way asyncpg's does:
    shielded from the caller's cancellation, for ``reset_secs`` or until the
    connection is closed. With ``back_early`` the connection is back in the
    pool, its proxy released, before the release returns. With ``bounded``
    its acquisitions name it and their connection, as asyncpg's do, and its
    ``release(conn, timeout=...)`` is asyncpg's bounded one: past the
    timeout it closes the connection, which the pool then has back, and
    raises ``TimeoutError``. With ``fail_with`` a release that ends within
    its timeout raises that error once the pool has the connection back."""

    def __init__(
        self, *, reset_secs: float, back_early: bool = False, bounded: bool = False,
        fail_with: BaseException | None = None,
    ) -> None:
        self.reset_secs = reset_secs
        self.back_early = back_early
        self.bounded = bounded
        self.fail_with = fail_with
        self.raw = _RawConnection()
        self.back_at: float | None = None

    def acquire(self) -> "_ReleasingAcquire":
        return _ReleasingAcquire(self)

    async def release(self, conn: _PooledConnection, *, timeout: float | None = None) -> None:
        await asyncio.shield(conn.acquired._release(timeout))


class _ReleasingAcquire:
    def __init__(self, pool: _ReleasingPool) -> None:
        self._pool = pool
        self._proxy: _PooledConnection | None = None
        if pool.bounded:
            self.pool = pool
            self.connection: _PooledConnection | None = None
            self.done = False

    async def __aenter__(self) -> _PooledConnection:
        self._proxy = _PooledConnection(self._pool.raw)
        self._proxy.acquired = self
        if self._pool.bounded:
            self.connection = self._proxy
        return self._proxy

    async def __aexit__(self, *_exc) -> None:
        await asyncio.shield(self._release(None))

    async def _release(self, timeout: float | None) -> None:
        loop = asyncio.get_running_loop()
        assert self._proxy is not None
        if self._pool.back_early:
            # Back in the pool, for another request to take, before the
            # release has returned.
            self._proxy._raw = None
            self._pool.back_at = loop.time()
        reset = self._pool.reset_secs
        closed = asyncio.ensure_future(self._pool.raw.terminated.wait())
        await asyncio.wait({closed}, timeout=reset if timeout is None else min(reset, timeout))
        closed.cancel()
        timed_out = not self._pool.raw.terminated.is_set() and timeout is not None and timeout < reset
        if timed_out:
            self._pool.raw.terminate()
        self._proxy._raw = None
        if self._pool.back_at is None:
            self._pool.back_at = loop.time()
        if timed_out:
            raise TimeoutError("timed out resetting the connection")
        if self._pool.fail_with is not None:
            raise self._pool.fail_with


async def _a_plan_then_another(pool: _ReleasingPool) -> tuple[frozenset, float]:
    """A plan of `x` holds the only plan slot and gives its connection back
    through ``pool``; a plan of `y` waits for the slot. Returns the first
    plan and when the second one got the slot."""
    executor = user_sql_executor.UserSqlExecutor(pool)
    first = asyncio.create_task(executor._planned(_statement("x", slow=False)))
    await asyncio.sleep(0.01)
    second_started: list[float] = []

    async def second() -> None:
        async with user_sql_executor._plan_slot(["y"]):
            second_started.append(asyncio.get_running_loop().time())

    waiting = asyncio.create_task(second())
    plan = await asyncio.wait_for(first, timeout=5)
    await asyncio.wait_for(waiting, timeout=5)
    return plan, second_started[0]


async def test_a_plan_keeps_its_slot_until_its_slow_reset_is_done(monkeypatch):
    """A plan is read well within its 0.2 s deadline, and the pool's reset
    of its connection then takes 0.35 s, past that deadline and within the
    half-second grace. The plan is returned, and keeps its slot until the
    connection is back in the pool: the statement waiting for the slot
    starts only then. The deadline used to cancel the wait for the release,
    whose shielded reset went on while the slot went to the next statement."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "akb_sql_plan_timeout_ms", 200)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    pool = _ReleasingPool(reset_secs=0.35)
    plan, second_started = await _a_plan_then_another(pool)
    assert plan == frozenset()
    assert pool.back_at is not None and second_started >= pool.back_at
    assert not pool.raw.terminated.is_set()
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_reset_past_the_grace_closes_the_connection(monkeypatch):
    """A plan is read at once, and the pool's reset of its connection would
    not end. The release is given the grace, 0.1 s here, as its bound, which
    closes the connection and ends the reset; the plan gives up its slot
    once the pool has the connection back, and is returned. There used to be
    no grace: the slot was held until the plan's deadline and then given up
    with the reset still running."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "akb_sql_plan_timeout_ms", 2000)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    monkeypatch.setattr(user_sql_executor, "_PLAN_CLEANUP_SECS", 0.1)
    pool = _ReleasingPool(reset_secs=30.0, bounded=True)
    loop = asyncio.get_running_loop()
    started = loop.time()
    plan, second_started = await _a_plan_then_another(pool)
    assert plan == frozenset()
    assert pool.raw.terminated.is_set()
    assert pool.back_at is not None and second_started >= pool.back_at
    assert second_started - started < 0.6, second_started - started


async def test_a_connection_back_in_the_pool_is_never_closed(monkeypatch):
    """The pool has the plan's connection back, its proxy released and free
    for another request, while the release has not returned yet, and the
    grace runs out then. The plan does not close the connection, which is
    no longer its own, and gives up its slot."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "akb_sql_plan_timeout_ms", 2000)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    monkeypatch.setattr(user_sql_executor, "_PLAN_CLEANUP_SECS", 0.1)
    pool = _ReleasingPool(reset_secs=0.3, back_early=True)
    plan, _second_started = await _a_plan_then_another(pool)
    assert plan == frozenset()
    assert not pool.raw.terminated.is_set()
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_cancelled_plan_closes_its_connection_before_any_rollback(monkeypatch):
    """A plan's EXPLAIN is under way when its request is cancelled, and the
    rollback of its transaction would take two seconds on an open
    connection. The plan closes its connection first, so the rollback ends at
    once, and gives up its slot promptly. The transaction used to roll back
    first, and the connection was closed only then."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "akb_sql_plan_timeout_ms", 5000)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    slow = _SlowConn(explain_secs=5.0, slow_rollback=2.0)
    executor = user_sql_executor.UserSqlExecutor(_ConnPool(slow))
    loop = asyncio.get_running_loop()
    plan = asyncio.create_task(executor._planned(_statement("x", slow=True)))
    await asyncio.sleep(0.1)
    cancelled_at = loop.time()
    plan.cancel()
    done, _pending = await asyncio.wait({plan}, timeout=0.5)
    if not done:
        await asyncio.wait({plan})
        pytest.fail("the cancelled plan waited for its rollback")
    assert plan.cancelled()
    assert slow.terminated_at is not None and slow.terminated_at - cancelled_at < 0.1
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])


async def test_a_plan_read_whose_release_fails_is_not_write_busy(monkeypatch):
    """A plan is read well within its deadline, and giving its connection
    back then fails, before the release's bound, with a socket's
    `ETIMEDOUT`. The plan did not run out of time and nothing waited for
    admission, so the statement fails with that error, which the API reports
    as a transient dependency failure (`operation_timeout`), as an error
    giving back a connection is reported on other paths, and not as
    `write_busy`; the plan's slot is free again. The release's
    `TimeoutError` used to be taken for the plan's deadline."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "akb_sql_plan_timeout_ms", 2000)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    failure = TimeoutError(errno.ETIMEDOUT, "Connection timed out")
    pool = _ReleasingPool(reset_secs=0.05, bounded=True, fail_with=failure)
    executor = user_sql_executor.UserSqlExecutor(pool)
    with pytest.raises(TimeoutError) as raised:
        await asyncio.wait_for(executor._planned(_statement("x", slow=False)), timeout=5)
    assert raised.value is failure
    mapped = transient_error(raised.value)
    assert mapped is not None and mapped.code == OPERATION_TIMEOUT
    assert (_slots().running, _slots().busy, _slots().waiters) == (0, set(), [])
