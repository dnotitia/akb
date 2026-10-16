"""Pure policy and fail-closed route checks for shared password budgets."""
from __future__ import annotations

import asyncpg
from fastapi import HTTPException
import pytest

from app.api.routes import public
from app.services import publication_rate_limit as rl

_NOW = 10_000.0


def test_crossing_attempt_proceeds_but_followers_are_locked():
    budget = rl._Budget()
    for _ in range(rl._FREE_IP + 1):
        assert rl._reserve(budget, "ip", _NOW) == 0
    assert rl._reserve(budget, "ip", _NOW) == 10


def test_correct_password_on_boundary_recovers_via_release():
    budget = rl._Budget()
    for _ in range(rl._FREE_IP + 1):
        assert rl._reserve(budget, "ip", _NOW) == 0
    rl._release(budget, "ip")
    assert rl._reserve(budget, "ip", _NOW) == 0


def test_per_publication_backstop_throttles_ip_rotation():
    budget = rl._Budget()
    for i in range(rl._FREE_SLUG + 1):
        assert rl._reserve(budget, str(i), _NOW) == 0
    assert rl._reserve(budget, "new-ip", _NOW) == 10
    assert budget.total.failures == 31
    assert len(budget.ips) == 31


def test_quiet_hour_decays_both_counters_and_expired_ip_storage():
    budget = rl._Budget()
    for _ in range(rl._FREE_IP + 1):
        rl._reserve(budget, "ip", _NOW)
    assert rl._reserve(budget, "other", _NOW + rl._DECAY_SECS + 1) == 0
    assert budget.total.failures == 1
    assert set(budget.ips) == {"other"}


def test_backoff_expiry_keeps_escalation_without_extending_on_locked_retries():
    budget = rl._Budget()
    for _ in range(rl._FREE_IP + 1):
        rl._reserve(budget, "ip", _NOW)
    assert rl._reserve(budget, "ip", _NOW + 5) == 5
    assert budget.ips["ip"].failures == 6
    assert budget.ips["ip"].last == _NOW
    assert rl._reserve(budget, "ip", _NOW + 10) == 0
    assert rl._reserve(budget, "ip", _NOW + 10) == 30


def test_other_publications_are_independent():
    first, second = rl._Budget(), rl._Budget()
    for _ in range(rl._FREE_IP + 1):
        rl._reserve(first, "ip", _NOW)
    assert rl._reserve(first, "ip", _NOW) > 0
    assert rl._reserve(second, "ip", _NOW) == 0


def test_capacity_never_evicts_an_active_ip_lock_or_the_global_budget():
    budget = rl._Budget(ips={
        str(i): rl._E(failures=6, locked_until=_NOW + 30, last=_NOW)
        for i in range(rl._MAX_IP_ENTRIES)
    })
    assert rl._reserve(budget, "new-ip", _NOW) == 30
    assert len(budget.ips) == rl._MAX_IP_ENTRIES
    assert "new-ip" not in budget.ips
    budget.ips["old-unlocked"] = budget.ips.pop("0")
    budget.ips["old-unlocked"].locked_until = _NOW - 1
    assert rl._reserve(budget, "new-ip", _NOW) == 0
    assert len(budget.ips) == rl._MAX_IP_ENTRIES
    assert "old-unlocked" not in budget.ips
    assert "1" in budget.ips


def test_serialization_preserves_accounting_without_a_process_cache():
    budget = rl._Budget()
    for _ in range(rl._FREE_IP + 1):
        rl._reserve(budget, "ip", _NOW)
    from dataclasses import asdict

    restored = rl._Budget.load(asdict(budget))
    assert rl._reserve(restored, "ip", _NOW + 1) == 9


@pytest.mark.asyncio
async def test_unavailable_reservation_returns_503_before_password_verification(monkeypatch):
    async def unavailable(*_args):
        raise rl.BudgetUnavailable

    async def must_not_verify(*_args, **_kwargs):
        pytest.fail("password verification ran without a shared reservation")

    monkeypatch.setattr(rl, "reserve", unavailable)
    monkeypatch.setattr(public.publication_service, "resolve_publication", must_not_verify)
    with pytest.raises(HTTPException) as error:
        await public._attempt_password_resolve(
            "slug", password="not-a-secret", ip="ip", increment_view=False,  # pragma: allowlist secret -- synthetic rejected input
        )
    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_db_reservation_fails_closed_but_failed_refund_does_not_fail_a_verified_view(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        raise asyncpg.CannotConnectNowError("synthetic dependency outage")

    monkeypatch.setattr(rl, "_change", unavailable)
    with pytest.raises(rl.BudgetUnavailable):
        await rl.reserve("slug", "ip")
    await rl.release("slug", "ip")


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass,password", [(False, None), (True, "ignored")])
async def test_hmac_or_no_password_path_does_not_consume_a_budget(monkeypatch, bypass, password):
    async def must_not_reserve(*_args):
        pytest.fail("non-password path touched shared accounting")

    async def resolve(*_args, **_kwargs):
        return {"id": "publication"}

    monkeypatch.setattr(rl, "reserve", must_not_reserve)
    monkeypatch.setattr(public.publication_service, "resolve_publication", resolve)
    result = await public._attempt_password_resolve(
        "slug", password=password, ip="ip", increment_view=False, bypass_password=bypass,
    )
    assert result["id"] == "publication"
