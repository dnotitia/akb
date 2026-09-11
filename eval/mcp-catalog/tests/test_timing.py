from __future__ import annotations

import pytest

from mcp_catalog.timing import TimingTracker


def test_timing_snapshot_partitions_overlapping_work_without_double_counting() -> None:
    tracker = TimingTracker(started_at=100.0)
    tracker.record("model_execution", 100.1, 100.8)
    tracker.record("fixture_reset", 100.5, 100.7)
    tracker.record("checkpoint", 100.85, 100.9)

    snapshot = tracker.snapshot(attempt_index=3, finished_at=101.0)
    breakdown = snapshot["breakdown"]

    assert snapshot["attempt_index"] == 3
    assert snapshot["wall_seconds"] == pytest.approx(1.0)
    assert breakdown["fixture_reset"] == pytest.approx(0.2)
    assert breakdown["checkpoint"] == pytest.approx(0.05)
    assert sum(breakdown.values()) == pytest.approx(snapshot["wall_seconds"])


def test_timing_snapshot_assigns_uninstrumented_gaps_to_other() -> None:
    tracker = TimingTracker(started_at=50.0)
    tracker.record("model_execution", 50.25, 50.5)

    snapshot = tracker.snapshot(attempt_index=1, finished_at=51.0)

    assert snapshot["breakdown"]["other"] == pytest.approx(0.75)


def test_provider_wait_is_retained_as_a_distinct_breakdown() -> None:
    tracker = TimingTracker(started_at=10.0)
    tracker.record("provider_wait", 10.1, 10.4)

    snapshot = tracker.snapshot(attempt_index=1, finished_at=10.5)

    assert snapshot["breakdown"]["provider_wait"] == pytest.approx(0.3)
    assert sum(snapshot["breakdown"].values()) == pytest.approx(0.5)
