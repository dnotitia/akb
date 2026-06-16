"""Unit coverage for the auto vault_id backfill worker (issue #189 Phase 2).

The worker's job is to make the vault-filter path zero-touch: it backfills
`vault_id` onto pre-upgrade pgvector points on startup, and search reads
`is_ready()` to decide whether the vault path is safe yet. These tests lock the
contract that gates that decision — the DB-backed `_process_once` join is
exercised end-to-end in the e2e suite, not here.
"""
from __future__ import annotations

import pytest

from app.services import vault_backfill


@pytest.fixture(autouse=True)
def _reset_ready(monkeypatch):
    # `_ready` is module state that latches True for the process; isolate each test.
    monkeypatch.setattr(vault_backfill, "_ready", False, raising=False)


def test_is_ready_defaults_false_and_reflects_module_state(monkeypatch):
    assert vault_backfill.is_ready() is False
    monkeypatch.setattr(vault_backfill, "_ready", True, raising=False)
    assert vault_backfill.is_ready() is True


def test_applicable_only_for_pgvector_same_instance(monkeypatch):
    from app.config import settings

    # pgvector + blank dsn (index shares the main DB) → the auto-join backfill runs.
    monkeypatch.setattr(settings, "vector_store_driver", "pgvector", raising=False)
    monkeypatch.setattr(settings, "vector_store_dsn", "", raising=False)
    assert vault_backfill._applicable() is True

    # separate vector instance → the server-side join can't reach it → no-op.
    monkeypatch.setattr(settings, "vector_store_dsn", "postgres://other/db", raising=False)
    assert vault_backfill._applicable() is False

    # any non-pgvector driver has no vault_id column at all.
    monkeypatch.setattr(settings, "vector_store_dsn", "", raising=False)
    monkeypatch.setattr(settings, "vector_store_driver", "qdrant", raising=False)
    assert vault_backfill._applicable() is False


@pytest.mark.asyncio
async def test_process_once_non_pgvector_latches_ready_without_db(monkeypatch):
    """Other drivers have no vault_id column and the vault path is never eligible
    for them, so readiness is moot — latch ready (stop looping), never touch DB."""
    monkeypatch.setattr(vault_backfill, "_is_pgvector", lambda: False)

    async def _boom():
        raise AssertionError("get_pool called for a non-pgvector driver")

    monkeypatch.setattr(vault_backfill, "get_pool", _boom)
    assert await vault_backfill._process_once() == 0
    assert vault_backfill.is_ready() is True


@pytest.mark.asyncio
async def test_process_once_separate_instance_gates_on_driver_count(monkeypatch):
    """Separate pgvector instance: never auto-backfills (no server-side join) and
    never touches the main pool — readiness follows the driver's NULL count, so
    the manual-script escape hatch still activates the vault path (and only once
    the backfill is actually done)."""
    monkeypatch.setattr(vault_backfill, "_is_pgvector", lambda: True)
    monkeypatch.setattr(vault_backfill, "_same_instance", lambda: False)

    async def _boom():
        raise AssertionError("get_pool (main DB) called for a separate instance")

    monkeypatch.setattr(vault_backfill, "get_pool", _boom)

    pending = {"n": 5}

    class _Store:
        async def vault_backfill_pending(self):
            return pending["n"]

    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: _Store())

    # Backfill not yet run on the separate instance → stays gated.
    assert await vault_backfill._process_once() == 0
    assert vault_backfill.is_ready() is False

    # Operator ran the manual script → count hits 0 → readiness latches.
    pending["n"] = 0
    assert await vault_backfill._process_once() == 0
    assert vault_backfill.is_ready() is True


@pytest.mark.asyncio
async def test_process_once_short_circuits_once_ready(monkeypatch):
    """After readiness latches, the step is a pure memory check — no DB work."""
    monkeypatch.setattr(vault_backfill, "_ready", True, raising=False)
    monkeypatch.setattr(vault_backfill, "_applicable", lambda: True)

    async def _boom():
        raise AssertionError("get_pool called after ready")

    monkeypatch.setattr(vault_backfill, "get_pool", _boom)
    assert await vault_backfill._process_once() == 0


@pytest.mark.asyncio
async def test_pending_stats_shape(monkeypatch):
    """/health consumes this: always carries ready + applicable; null_remaining
    only when the driver exposes the counter."""
    monkeypatch.setattr(vault_backfill, "_applicable", lambda: True)

    class _Store:
        async def vault_backfill_pending(self):
            return 42

    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: _Store())
    stats = await vault_backfill.pending_stats()
    assert stats["ready"] is False
    assert stats["applicable"] is True
    assert stats["null_remaining"] == 42


@pytest.mark.asyncio
async def test_pending_stats_omits_count_for_drivers_without_counter(monkeypatch):
    class _Store:  # no vault_backfill_pending attribute
        pass

    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: _Store())
    stats = await vault_backfill.pending_stats()
    assert "null_remaining" not in stats
    assert set(stats) == {"ready", "applicable"}
