"""Deterministic wall-clock accounting for benchmark attempts."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Iterator, Literal


TimingCategory = Literal[
    "provisioning",
    "credential",
    "catalog_capture",
    "fixture_reset",
    "provider_wait",
    "model_execution",
    "checkpoint",
    "other",
]
TIMING_CATEGORIES: tuple[TimingCategory, ...] = (
    "provisioning",
    "credential",
    "catalog_capture",
    "fixture_reset",
    "provider_wait",
    "model_execution",
    "checkpoint",
    "other",
)
# When parallel work overlaps, the higher-priority operation owns that segment
# of wall time. The sum is therefore the elapsed wall time, not double-counted
# worker time.
_TIMING_PRIORITY: tuple[TimingCategory, ...] = (
    "provisioning",
    "credential",
    "catalog_capture",
    "fixture_reset",
    "provider_wait",
    "checkpoint",
    "model_execution",
    "other",
)


@dataclass(frozen=True, slots=True)
class TimingInterval:
    category: TimingCategory
    started_at: float
    finished_at: float


@dataclass(slots=True)
class TimingTracker:
    """Collect intervals and partition each attempt's wall time exactly once."""

    started_at: float = field(default_factory=time.perf_counter)
    intervals: list[TimingInterval] = field(default_factory=list)

    def record(self, category: TimingCategory, started_at: float, finished_at: float | None = None) -> None:
        finished = time.perf_counter() if finished_at is None else finished_at
        if finished < started_at:
            raise ValueError("timing interval finished before it started")
        self.intervals.append(TimingInterval(category, started_at, finished))

    @contextlib.contextmanager
    def measure(self, category: TimingCategory) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(category, started)

    def snapshot(self, *, attempt_index: int, finished_at: float | None = None) -> dict[str, object]:
        finished = time.perf_counter() if finished_at is None else finished_at
        total = max(0.0, finished - self.started_at)
        boundaries = {0.0, total}
        intervals: list[tuple[TimingCategory, float, float]] = []
        for interval in self.intervals:
            start = max(0.0, interval.started_at - self.started_at)
            end = min(total, interval.finished_at - self.started_at)
            if end <= start:
                continue
            intervals.append((interval.category, start, end))
            boundaries.update((start, end))

        values: dict[str, float] = {category: 0.0 for category in TIMING_CATEGORIES}
        ordered_boundaries = sorted(boundaries)
        for start, end in zip(ordered_boundaries, ordered_boundaries[1:]):
            if end <= start:
                continue
            active = {
                category
                for category, interval_start, interval_end in intervals
                if interval_start <= start and interval_end >= end
            }
            owner = next((category for category in _TIMING_PRIORITY if category in active), "other")
            values[owner] += end - start
        values["other"] += max(0.0, total - sum(values.values()))
        return {
            "attempt_index": attempt_index,
            "wall_seconds": total,
            "breakdown": values,
        }
