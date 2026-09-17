"""A skipped BM25 recompute is not a finished one.

`BackfillRunner` offers two outcomes: return 0 and sleep the configured
interval, or return non-zero and drain immediately. The refresher's interval is
six hours, so returning 0 after failing to take the lock pays six hours for a
tick that did nothing — and there is one case where that is the wrong price.

During a rolling restart the replaced pod keeps its PostgreSQL session, and so
the advisory lock, until it drains. The replacement's first tick lands inside
that window and finds the lock held by a process that is already leaving. Three
restarts in a day is three intervals lost, which is how a deployment reached
`last_recomputed_at` more than fifteen hours old with the corpus moved on.

A lock still held after a short bounded wait belongs to a recompute that is
really running on another replica. Skipping that one is correct: it publishes
the very stats this tick wanted. These assertions pin both halves.
"""

import pytest

from app.services import sparse_encoder


def _stats(*, skipped: bool) -> dict:
    return {
        "total_docs": None if skipped else 10,
        "avgdl": None if skipped else 1.0,
        "vocab_size": None if skipped else 5,
        "tokenizer": "kiwi@test",
        "skipped": skipped,
    }


@pytest.fixture
def refresher(monkeypatch):
    """Drive the tick with a scripted `recompute_stats` and no real sleeping."""
    calls: list[str] = []
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(sparse_encoder.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        sparse_encoder, "_should_recompute", _always(True, calls, "should_recompute")
    )
    return calls, slept


def _always(value, calls, name):
    async def _fn(*_args, **_kwargs):
        calls.append(name)
        return value

    return _fn


def _scripted(outcomes, calls, monkeypatch):
    remaining = list(outcomes)

    async def _recompute(*_args, **_kwargs):
        calls.append("recompute")
        return _stats(skipped=remaining.pop(0) if remaining else False)

    monkeypatch.setattr(sparse_encoder, "recompute_stats", _recompute)


@pytest.mark.asyncio
async def test_a_tick_with_nothing_to_do_does_not_recompute(monkeypatch, refresher):
    calls, slept = refresher
    monkeypatch.setattr(
        sparse_encoder, "_should_recompute", _always(False, calls, "should_recompute")
    )
    _scripted([], calls, monkeypatch)

    assert await sparse_encoder._refresh_tick(retry_secs=0) == 0
    assert "recompute" not in calls
    assert slept == []


@pytest.mark.asyncio
async def test_a_handover_skip_is_retried_rather_than_costing_an_interval(
    monkeypatch, refresher
):
    """The draining pod releases its lock; the next attempt takes it."""
    calls, slept = refresher
    _scripted([True, False], calls, monkeypatch)

    assert await sparse_encoder._refresh_tick(retry_secs=7) == 0
    assert calls.count("recompute") == 2
    assert slept == [7]


@pytest.mark.asyncio
async def test_a_peer_that_is_really_working_is_left_alone(monkeypatch, refresher):
    """Bounded: we outlast a handover, we do not wait out a peer's recompute."""
    calls, slept = refresher
    _scripted([True] * 10, calls, monkeypatch)

    assert await sparse_encoder._refresh_tick(retry_secs=3) == 0
    assert calls.count("recompute") == sparse_encoder._SKIPPED_RETRIES + 1
    # One wait between attempts, never after the last one.
    assert slept == [3] * sparse_encoder._SKIPPED_RETRIES


@pytest.mark.asyncio
async def test_the_tick_always_reports_idle(monkeypatch, refresher):
    """Returning non-zero would make `BackfillRunner` drain in a tight loop."""
    calls, _slept = refresher
    for outcomes in ([False], [True, False], [True] * 10):
        _scripted(outcomes, calls, monkeypatch)
        assert await sparse_encoder._refresh_tick(retry_secs=0) == 0
