"""The registered paired cluster BCa confidence interval."""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
from scipy.stats import bootstrap  # type: ignore[import-untyped]


class InconclusiveBootstrap(ValueError):
    """The registered BCa interval cannot be computed from these samples."""


def paired_cluster_bca(
    baseline: list[float],
    candidate: list[float],
    *,
    alternative: Literal["greater", "less"] = "greater",
    confidence: float = 0.95,
    resamples: int = 20_000,
    seed: int = 358,
) -> dict[str, float | int | str]:
    """Compute a one-sided paired BCa interval over equal-weight clusters."""

    if len(baseline) != len(candidate) or len(baseline) < 3:
        raise InconclusiveBootstrap("paired cluster samples are incomplete")
    if not all(math.isfinite(value) for value in (*baseline, *candidate)):
        raise InconclusiveBootstrap("paired cluster samples contain non-finite values")
    baseline_array = np.asarray(baseline, dtype=np.float64)
    candidate_array = np.asarray(candidate, dtype=np.float64)

    def mean_difference(base: np.ndarray, cand: np.ndarray, *, axis: int) -> np.ndarray:
        return np.mean(cand - base, axis=axis)

    try:
        interval = bootstrap(
            (baseline_array, candidate_array),
            mean_difference,
            paired=True,
            vectorized=True,
            axis=0,
            n_resamples=resamples,
            batch=512,
            confidence_level=confidence,
            alternative=alternative,
            method="BCa",
            rng=np.random.default_rng(seed),
        ).confidence_interval
    except (ValueError, FloatingPointError, RuntimeError) as exc:
        raise InconclusiveBootstrap(f"BCa interval failed: {type(exc).__name__}") from exc

    low, high = float(interval.low), float(interval.high)
    # A one-sided SciPy result intentionally uses infinity at the unbounded
    # side. NaN at either side signals a degenerate BCa acceleration.
    if math.isnan(low) or math.isnan(high):
        raise InconclusiveBootstrap("BCa interval is degenerate")
    return {
        "baseline_mean": float(np.mean(baseline_array)),
        "candidate_mean": float(np.mean(candidate_array)),
        "difference_candidate_minus_baseline": float(np.mean(candidate_array - baseline_array)),
        "lower_bound": low,
        "upper_bound": high,
        "independent_clusters": len(baseline),
        "method": "paired_cluster_bca_bootstrap",
        "confidence": confidence,
        "resamples": resamples,
        "seed": seed,
        "alternative": alternative,
    }
