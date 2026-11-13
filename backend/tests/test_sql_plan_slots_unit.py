"""Raw SQL plans are read a few at a time, and Vaults take turns for them.

A raw SQL write reads its plan on a pool connection before its admission. At
most `akb_sql_plan_concurrency` plans are read at once per process. Waiters
queue by the Vaults their statement declares, and a free slot goes to the
queues in turn, so the plans of one Vault whose table is locked, however many,
do not keep another Vault's statements waiting until their deadline.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.exceptions import WriteBusyError
from app.services import user_sql_executor


def _running() -> int:
    return user_sql_executor._plan_slots[asyncio.get_running_loop()].running


async def test_plans_are_capped_and_vaults_take_turns(monkeypatch):
    """Six plans of one Vault wait, then one of another Vault. At most two
    are read at once, and the other Vault's plan is read next but one."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    order: list[str] = []
    reading = peak = 0

    async def plan(vaults: list[str], label: str) -> None:
        nonlocal reading, peak
        async with user_sql_executor._plan_slot(vaults):
            reading += 1
            peak = max(peak, reading)
            order.append(label)
            await asyncio.sleep(0.02)
            reading -= 1

    busy = [asyncio.create_task(plan(["busy"], f"busy-{i}")) for i in range(6)]
    await asyncio.sleep(0)
    other = asyncio.create_task(plan(["quiet"], "quiet"))
    await asyncio.gather(*busy, other)
    assert peak == 2
    # Two plans of the busy Vault were read at once; when they finish, one
    # more of it and then the other Vault's.
    assert order.index("quiet") == 3, order
    assert _running() == 0


async def test_a_statement_declaring_several_vaults_queues_once(monkeypatch):
    """The Vaults a statement declares, in any order and repeated, name one
    queue: its second statement waits for that queue's next turn, after
    another queue's statement that arrived later."""
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    order: list[str] = []
    release = asyncio.Event()
    holding = asyncio.Event()

    async def hold() -> None:
        async with user_sql_executor._plan_slot(["other"]):
            holding.set()
            await release.wait()

    async def plan(vaults: list[str], label: str) -> None:
        async with user_sql_executor._plan_slot(vaults):
            order.append(label)
            await asyncio.sleep(0.01)

    holder = asyncio.create_task(hold())
    await holding.wait()
    tasks = [
        asyncio.create_task(plan(["a", "b"], "ab-1")),
        asyncio.create_task(plan(["b", "a", "b"], "ab-2")),
        asyncio.create_task(plan(["c"], "c")),
    ]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(holder, *tasks)
    assert order == ["ab-1", "c", "ab-2"], order


async def test_a_plan_that_waits_past_its_deadline_is_write_busy_and_takes_no_slot(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.05)
    release = asyncio.Event()
    holding = asyncio.Event()

    async def hold() -> None:
        async with user_sql_executor._plan_slot(["busy"]):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await holding.wait()
    with pytest.raises(WriteBusyError) as refused:
        async with user_sql_executor._plan_slot(["quiet"]):
            pass
    assert refused.value.retry_after_secs == 1
    release.set()
    await holder
    async with asyncio.timeout(1.0):
        async with user_sql_executor._plan_slot(["quiet"]):
            assert _running() == 1
    assert _running() == 0


async def test_a_cancelled_plan_wait_takes_no_slot(monkeypatch):
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 5.0)
    release = asyncio.Event()
    holding = asyncio.Event()

    async def hold() -> None:
        async with user_sql_executor._plan_slot(["busy"]):
            holding.set()
            await release.wait()

    async def wait_for_a_slot() -> None:
        async with user_sql_executor._plan_slot(["quiet"]):
            pass

    holder = asyncio.create_task(hold())
    await holding.wait()
    waiter = asyncio.create_task(wait_for_a_slot())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await holder
    assert _running() == 0
    async with asyncio.timeout(1.0):
        async with user_sql_executor._plan_slot(["quiet"]):
            assert _running() == 1
