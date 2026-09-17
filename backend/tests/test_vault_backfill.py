"""Unit coverage for the auto vault_id backfill worker (issue #189 Phase 2).

The worker's job is to make the vault-filter path zero-touch: it backfills
`vault_id` onto pre-upgrade pgvector points on startup, and search reads
readiness to decide whether the vault path is safe yet. These tests lock the
contract that gates that decision — the DB-backed `_process_once` join is
exercised end-to-end in the e2e suite, not here.

akb#526: readiness must be visible to the SERVING tier, which never runs the
backfill runner on a split api/worker deployment. `is_ready()` is the
process-local latch (worker loop + fast path); `is_ready_async()` derives it
from store state both tiers can see, with a short negative-result cache.
"""
from __future__ import annotations

import pytest

from app.services import vault_backfill


@pytest.fixture(autouse=True)
def _reset_ready(monkeypatch):
    # `_ready` / `_last_check` are module state; isolate each test.
    monkeypatch.setattr(vault_backfill, "_ready", False, raising=False)
    monkeypatch.setattr(vault_backfill, "_last_check", None, raising=False)


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
async def test_process_once_non_capable_latches_ready_without_db(monkeypatch):
    """A driver without the vault filter (vault_filter_supported absent/False)
    never takes the vault path, so readiness is moot — latch ready (stop looping),
    never touch DB."""
    class _Plain:  # no vault_filter_supported attribute
        pass

    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: _Plain())

    async def _boom():
        raise AssertionError("get_pool called for a non-capable driver")

    monkeypatch.setattr(vault_backfill, "get_pool", _boom)
    assert await vault_backfill._process_once() == 0
    assert vault_backfill.is_ready() is True


@pytest.mark.asyncio
async def test_process_once_capable_non_autofill_gates_on_count(monkeypatch):
    """A capable driver this worker can't auto-fill (qdrant, seahorse, or a
    separate pgvector instance): never touches the main pool — readiness follows
    the driver's NULL count, so the manual-backfill escape hatch still activates
    the vault path (and only once the backfill is actually done)."""
    # Not same-instance pgvector → _applicable() is False → the count-gate branch.
    monkeypatch.setattr(vault_backfill, "_is_pgvector", lambda: True)
    monkeypatch.setattr(vault_backfill, "_same_instance", lambda: False)

    async def _boom():
        raise AssertionError("get_pool (auto-join) called for a gate-only driver")

    monkeypatch.setattr(vault_backfill, "get_pool", _boom)

    pending = {"n": 5}

    class _Store:
        vault_filter_supported = True

        async def vault_backfill_pending(self):
            return pending["n"]

    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: _Store())

    # Backfill not yet run → stays gated.
    assert await vault_backfill._process_once() == 0
    assert vault_backfill.is_ready() is False

    # Operator ran the manual backfill → count hits 0 → readiness latches.
    pending["n"] = 0
    assert await vault_backfill._process_once() == 0
    assert vault_backfill.is_ready() is True


@pytest.mark.asyncio
async def test_process_once_capable_without_counter_stays_gated(monkeypatch):
    """A capable driver (e.g. seahorse gRPC/cloud) that exposes no
    vault_backfill_pending() can't prove its existing points carry vault_id, so
    the worker keeps it gated forever — never activate the vault path blind."""
    monkeypatch.setattr(vault_backfill, "_is_pgvector", lambda: False)

    class _Store:
        vault_filter_supported = True  # capable, but NO vault_backfill_pending

    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: _Store())
    assert await vault_backfill._process_once() == 0
    assert vault_backfill.is_ready() is False


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
    assert set(stats) == {"ready", "applicable", "vault_filter_supported"}


# ── akb#526: cross-process readiness for the serving tier ──

class _CountedStore:
    """Driver stand-in with an observable NULL-vault_id count."""

    vault_filter_supported = True

    def __init__(self, pending: int, fail: bool = False):
        self.pending = pending
        self.fail = fail
        self.calls = 0

    async def vault_backfill_pending(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("store unreachable")
        return self.pending


@pytest.mark.asyncio
async def test_is_ready_async_opens_path_without_worker_latch(monkeypatch):
    """The serving tier never runs the backfill runner, so its `_ready` is
    False forever. A zero NULL count from the store must still open the path
    (and latch the local fast path)."""
    store = _CountedStore(pending=0)
    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: store)
    assert vault_backfill.is_ready() is False
    assert await vault_backfill.is_ready_async() is True
    assert vault_backfill.is_ready() is True
    assert store.calls == 1


@pytest.mark.asyncio
async def test_is_ready_async_caches_negative_outcome(monkeypatch):
    """A nonzero count stays cached for the TTL: the hot path must not pay a
    COUNT(*) per search while the backfill is still draining."""
    store = _CountedStore(pending=5)
    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: store)
    assert await vault_backfill.is_ready_async() is False
    assert await vault_backfill.is_ready_async() is False
    assert store.calls == 1


@pytest.mark.asyncio
async def test_is_ready_async_rechecks_after_ttl(monkeypatch):
    """After the TTL the count is re-read, so the path opens within ~30s of
    the backfill completing anywhere (any process, any replica)."""
    store = _CountedStore(pending=5)
    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: store)
    assert await vault_backfill.is_ready_async() is False
    assert store.calls == 1
    monkeypatch.setattr(vault_backfill, "_NEGATIVE_TTL_SECS", 0)
    store.pending = 0
    assert await vault_backfill.is_ready_async() is True
    assert store.calls == 2


@pytest.mark.asyncio
async def test_is_ready_async_stays_gated_on_counter_failure(monkeypatch):
    """A counter failure must not open the path — fail closed, stay cached."""
    store = _CountedStore(pending=0, fail=True)
    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: store)
    assert await vault_backfill.is_ready_async() is False
    assert await vault_backfill.is_ready_async() is False
    assert vault_backfill.is_ready() is False
    assert store.calls == 1


@pytest.mark.asyncio
async def test_is_ready_async_without_counter_stays_gated(monkeypatch):
    """A capable driver with no vault_backfill_pending can't prove its points
    carry vault_id — same fail-closed rule as the worker branch."""

    class _NoCounter:
        vault_filter_supported = True

    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: _NoCounter())
    assert await vault_backfill.is_ready_async() is False
    assert vault_backfill.is_ready() is False


@pytest.mark.asyncio
async def test_is_ready_async_non_capable_latches_immediately(monkeypatch):
    """A driver without the vault filter never takes the vault path, so the
    check is moot — same immediate-latch rule as the worker branch, no store
    call needed."""

    class _Plain:  # no vault_filter_supported attribute
        pass

    monkeypatch.setattr(vault_backfill, "get_vector_store", lambda: _Plain())
    assert await vault_backfill.is_ready_async() is True
    assert vault_backfill.is_ready() is True
