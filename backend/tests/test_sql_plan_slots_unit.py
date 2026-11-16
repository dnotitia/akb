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
import time

import pytest

from app.config import settings
from app.exceptions import WriteBusyError
from app.services import user_sql_executor


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
