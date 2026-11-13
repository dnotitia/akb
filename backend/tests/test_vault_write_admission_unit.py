"""Unit tests for the per-process Vault write admission.

No database: these check the gate itself. That transactions of one Vault
beyond the limit wait without blocking another Vault, that a wait past the
deadline is a retryable `write_busy` and leaves nothing behind, that admission
is re-entrant within a task, that several Vaults are admitted together, and
that an event written without admission is logged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

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
    assert admission.snapshot() == {"vaults": 0, "transactions": 0}


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
    # Only the two holders remain; the timed-out waiter left no count behind.
    assert admission.snapshot() == {"vaults": 1, "transactions": 2}
    release.set()
    await asyncio.gather(*holders)
    assert admission.snapshot() == {"vaults": 0, "transactions": 0}


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
    # The slot it took for `first` was given back.
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(first):
            pass
    release.set()
    await holder
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(first, second):
            assert admission.is_admitted(first) and admission.is_admitted(second)


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
    assert admission.snapshot() == {"vaults": 0, "transactions": 0}
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(vault):
            pass


async def test_no_vault_is_no_admission():
    async with asyncio.timeout(1):
        async with admission.vault_write_admission(None):
            assert admission.snapshot() == {"vaults": 0, "transactions": 0}


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
