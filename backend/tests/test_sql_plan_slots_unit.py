"""Raw SQL plans are read a few at a time, and one at a time per Vault.

A raw SQL write reads its plan on a pool connection before its admission. At
most `akb_sql_plan_concurrency` plans are read at once per process, and a plan
holds a token for every Vault its statement declares, taken all at once or
not at all, so at most one plan per Vault is read at a time. Waiters hold
nothing. The plans of a Vault whose table is locked therefore wait one behind
another, whatever other Vaults they declare with it, and a statement of
another Vault takes the free slot. A wait that ends without a slot leaves the
queue at once.
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
    """A statement declaring `a` and `c` waits while `a` is busy, and holds
    nothing meanwhile: a statement declaring only `c` goes first. It is read
    once both are free."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    order: list[str] = []
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold(["a"], holding, release))
    await holding.wait()

    async def plan(vaults: list[str], label: str, seconds: float) -> None:
        async with user_sql_executor._plan_slot(vaults):
            order.append(label)
            await asyncio.sleep(seconds)

    both = asyncio.create_task(plan(["c", "a", "c"], "a+c", 0))
    await asyncio.sleep(0)
    only_c = asyncio.create_task(plan(["c"], "c", 0.05))
    await asyncio.sleep(0.01)
    assert order == ["c"]
    release.set()
    await asyncio.gather(holder, both, only_c)
    assert order == ["c", "a+c"]


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
