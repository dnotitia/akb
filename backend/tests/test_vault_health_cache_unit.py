"""Per-vault health results are reused briefly, never shared across failures,
and never stand in for the per-request access check.

`GET /health/vault/{name}` aggregates over the vault's whole chunk and queue
population, and the web console polls it for each of the user's vaults from
every open tab. These tests pin the cache's contract: a hit inside the TTL
performs no database work, expiry recomputes, concurrent misses compute once, a
failure is not reused, a zero TTL disables it, memory stays bounded, and the
route authorizes the caller before it looks at the cache.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import VAULT_HEALTH_CACHE_TTL_MAX_SECS, settings
from app.exceptions import ForbiddenError
from app.services import embed_worker, health, metadata_worker, native_derived_worker, native_file_projection
from app.services.health import VaultHealthCache


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _result(tag: str = "a") -> dict:
    return {
        "metadata_backfill": {"pending": 0, "retrying": 0, "abandoned": 0},
        "vector_store": {"backfill": {"upsert": {"pending": 1, "retrying": 0, "abandoned": 0, "indexed": 9}}},
        "native_file_projection": {"pending": 0},
        "native_derived": {"pending": 0, "tag": tag},
    }


async def test_hit_within_ttl_does_not_recompute():
    clock = _Clock()
    cache = VaultHealthCache(30.0, clock=clock)
    compute = AsyncMock(return_value=_result())
    vault_id = uuid.uuid4()

    first = await cache.get(vault_id, compute)
    clock.now += 29.9
    second = await cache.get(vault_id, compute)

    compute.assert_awaited_once_with(vault_id)
    assert first == second == _result()


async def test_hit_runs_no_database_query(monkeypatch):
    """The route's entry point, with every per-vault stats query replaced."""
    clock = _Clock()
    monkeypatch.setattr(health, "_vault_health_cache", VaultHealthCache(30.0, clock=clock))
    queries = {
        "chunks": AsyncMock(return_value={"upsert": {"pending": 0, "retrying": 0, "abandoned": 0, "indexed": 1}}),
        "metadata": AsyncMock(return_value={"pending": 0, "retrying": 0, "abandoned": 0}),
        "projection": AsyncMock(return_value={"pending": 0, "retrying": 0, "exhausted": 0, "abandoned": 0}),
        "derived": AsyncMock(return_value={"pending": 0, "retrying": 0, "exhausted": 0, "abandoned": 0}),
        "current_heads": AsyncMock(return_value={"pending": 0, "retrying": 0, "exhausted": 0, "abandoned": 0}),
    }
    monkeypatch.setattr(embed_worker, "pending_stats", queries["chunks"])
    monkeypatch.setattr(metadata_worker, "pending_stats", queries["metadata"])
    monkeypatch.setattr(native_file_projection, "pending_stats", queries["projection"])
    monkeypatch.setattr(native_derived_worker, "pending_stats", queries["derived"])
    monkeypatch.setattr(native_derived_worker, "current_head_stats", queries["current_heads"])
    vault_id = uuid.uuid4()

    first = await health.cached_vault_health(vault_id)
    clock.now += 10
    second = await health.cached_vault_health(vault_id)

    for query in queries.values():
        query.assert_awaited_once_with(vault_id)
    # Same payload, observation time included: a hit is the earlier
    # observation, not a fresh one relabelled.
    assert first == second
    assert set(first) >= {"metadata_backfill", "vector_store", "native_file_projection", "native_derived"}


async def test_expiry_recomputes():
    clock = _Clock()
    cache = VaultHealthCache(30.0, clock=clock)
    compute = AsyncMock(side_effect=[_result("old"), _result("new")])
    vault_id = uuid.uuid4()

    assert (await cache.get(vault_id, compute))["native_derived"]["tag"] == "old"
    clock.now += 30.0
    assert (await cache.get(vault_id, compute))["native_derived"]["tag"] == "new"
    assert compute.await_count == 2
    # The fresh result is what the next hit serves.
    assert (await cache.get(vault_id, compute))["native_derived"]["tag"] == "new"
    assert compute.await_count == 2


async def test_vaults_are_cached_separately():
    cache = VaultHealthCache(30.0, clock=_Clock())
    first, second = uuid.uuid4(), uuid.uuid4()

    async def compute(vault_id):
        return _result(str(vault_id))

    assert (await cache.get(first, compute))["native_derived"]["tag"] == str(first)
    assert (await cache.get(second, compute))["native_derived"]["tag"] == str(second)
    assert (await cache.get(first, compute))["native_derived"]["tag"] == str(first)
    assert len(cache) == 2


async def test_concurrent_misses_compute_once():
    cache = VaultHealthCache(30.0, clock=_Clock())
    release = asyncio.Event()
    calls = 0

    async def compute(vault_id):
        nonlocal calls
        calls += 1
        await release.wait()
        return _result()

    vault_id = uuid.uuid4()
    waiters = [asyncio.create_task(cache.get(vault_id, compute)) for _ in range(10)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*waiters)

    assert calls == 1
    assert all(result == _result() for result in results)
    # Every caller still gets its own object.
    assert len({id(result) for result in results}) == len(results)


async def test_failure_reaches_every_waiter_and_is_not_cached():
    cache = VaultHealthCache(30.0, clock=_Clock())
    release = asyncio.Event()
    calls = 0

    async def compute(vault_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
            raise RuntimeError("database unavailable")
        return _result("recovered")

    vault_id = uuid.uuid4()
    waiters = [asyncio.create_task(cache.get(vault_id, compute)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(*waiters, return_exceptions=True)

    assert calls == 1
    assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)
    assert len(cache) == 0
    # The next request computes again rather than replaying the failure.
    assert (await cache.get(vault_id, compute))["native_derived"]["tag"] == "recovered"
    assert calls == 2
    assert (await cache.get(vault_id, compute))["native_derived"]["tag"] == "recovered"
    assert calls == 2


async def test_zero_ttl_disables_the_cache():
    cache = VaultHealthCache(0.0, clock=_Clock())
    compute = AsyncMock(return_value=_result())
    vault_id = uuid.uuid4()

    await cache.get(vault_id, compute)
    await cache.get(vault_id, compute)

    assert compute.await_count == 2
    assert len(cache) == 0


async def test_a_caller_that_leaves_does_not_cancel_the_shared_computation():
    cache = VaultHealthCache(30.0, clock=_Clock())
    release = asyncio.Event()
    compute_calls = 0

    async def compute(vault_id):
        nonlocal compute_calls
        compute_calls += 1
        await release.wait()
        return _result()

    vault_id = uuid.uuid4()
    leaving = asyncio.create_task(cache.get(vault_id, compute))
    await asyncio.sleep(0)
    staying = asyncio.create_task(cache.get(vault_id, compute))
    await asyncio.sleep(0)
    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving
    release.set()

    assert await staying == _result()
    assert compute_calls == 1
    assert len(cache) == 1


async def test_callers_cannot_change_each_others_result():
    cache = VaultHealthCache(30.0, clock=_Clock())
    vault_id = uuid.uuid4()
    compute = AsyncMock(return_value=_result())

    # Once on the result of the miss that filled the entry, once on a hit.
    for _ in range(2):
        mine = await cache.get(vault_id, compute)
        mine["vector_store"]["backfill"]["upsert"]["pending"] = 999
        mine["injected"] = True

    assert await cache.get(vault_id, compute) == _result()
    compute.assert_awaited_once()


async def test_size_is_bounded_and_expired_entries_are_dropped():
    clock = _Clock()
    cache = VaultHealthCache(30.0, max_entries=3, clock=clock)
    compute = AsyncMock(side_effect=lambda vault_id: _result(str(vault_id)))
    vaults = [uuid.uuid4() for _ in range(5)]

    for vault_id in vaults:
        await cache.get(vault_id, compute)
        clock.now += 1
    assert len(cache) == 3

    # The two closest to expiry made room; the newest three are still hits.
    for vault_id in vaults[2:]:
        await cache.get(vault_id, compute)
    assert compute.await_count == 5
    await cache.get(vaults[0], compute)
    assert compute.await_count == 6

    # Entries past their TTL go as soon as anything new is stored.
    clock.now += 60
    await cache.get(uuid.uuid4(), compute)
    assert len(cache) == 1


def test_max_entries_must_be_positive():
    with pytest.raises(ValueError):
        VaultHealthCache(30.0, max_entries=0)


def test_ttl_comes_from_settings_without_an_override(monkeypatch):
    monkeypatch.delenv(health.VAULT_HEALTH_CACHE_TTL_ENV, raising=False)
    monkeypatch.setattr(settings, "vault_health_cache_ttl_secs", 12.5)
    assert health.configured_vault_health_cache_ttl() == 12.5
    monkeypatch.setenv(health.VAULT_HEALTH_CACHE_TTL_ENV, "   ")
    assert health.configured_vault_health_cache_ttl() == 12.5


def test_default_ttl_is_thirty_seconds():
    assert type(settings).model_fields["vault_health_cache_ttl_secs"].default == 30.0


@pytest.mark.parametrize(("raw", "expected"), [("0", 0.0), ("5", 5.0), (" 7.5 ", 7.5), ("3600", 3600.0)])
def test_environment_override_wins(monkeypatch, raw, expected):
    monkeypatch.setattr(settings, "vault_health_cache_ttl_secs", 30.0)
    monkeypatch.setenv(health.VAULT_HEALTH_CACHE_TTL_ENV, raw)
    assert health.configured_vault_health_cache_ttl() == expected


@pytest.mark.parametrize("raw", ["thirty", "-1", "nan", "inf", str(VAULT_HEALTH_CACHE_TTL_MAX_SECS + 1)])
def test_malformed_environment_override_fails_loudly(monkeypatch, raw):
    monkeypatch.setenv(health.VAULT_HEALTH_CACHE_TTL_ENV, raw)
    with pytest.raises(RuntimeError, match=health.VAULT_HEALTH_CACHE_TTL_ENV):
        health.configured_vault_health_cache_ttl()


@pytest.fixture
def api(monkeypatch, tmp_path):
    # Importing the API app constructs the legacy revision backend, which
    # creates its Git storage root eagerly. Redirect it before the import.
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    import app.main as main

    return main


async def test_route_authorizes_every_request_before_the_cache(api, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(health, "_vault_health_cache", VaultHealthCache(30.0, clock=clock))
    computed = AsyncMock(return_value=_result())
    monkeypatch.setattr(health, "vault_health", computed)
    vault_id = uuid.uuid4()
    access = AsyncMock(side_effect=[
        {"vault_id": vault_id},
        {"vault_id": vault_id},
        ForbiddenError("no longer a reader"),
    ])
    monkeypatch.setattr(api, "check_vault_access", access)
    user = SimpleNamespace(user_id=str(uuid.uuid4()))

    first = await api.vault_health_route("research", user=user)
    second = await api.vault_health_route("research", user=user)
    # The result for this vault is still cached, and still refused.
    with pytest.raises(ForbiddenError):
        await api.vault_health_route("research", user=user)

    assert first == second == {"vault": "research", **_result()}
    # One check per request, hits included. The cache key is the vault id the
    # check returns, so the check cannot move behind the cache either.
    assert access.await_count == 3
    for call in access.await_args_list:
        assert call.args == (user.user_id, "research")
        assert call.kwargs == {"required_role": "reader"}
    computed.assert_awaited_once_with(vault_id)
