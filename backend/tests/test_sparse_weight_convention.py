"""Cross-driver regression: every value in
``settings.vector_store_driver``'s Literal must map to a known sparse
weight convention.

Background: 0.7.7 fixed the "pre-baked TF×IDF gets double-saturated"
bug for the (then-only) seahorse-db driver by gating the encoder via
``_use_raw_weights()``. 0.8.0 added a sibling driver
(``seahorse-db-grpc``) that talks to the same Coral backend over a
different transport — and the gate's literal-match was forgotten in
the first cut, silently resurrecting the bug for the gRPC path.

A reviewer caught it. This test would have caught it too — and it
catches the next one, when a third or fourth Coral-family transport
shows up.

The rule: every driver enum value the project ships must be
classified as either ``"raw_tf"`` or ``"pre_baked"``, and the
encoder's ``_use_raw_weights()`` flag must agree with that
classification. If you add a new driver without updating
``_EXPECTED`` below, the test fails — which means you have to make
a deliberate decision about which convention the new driver expects.
"""
from __future__ import annotations

import typing

import pytest

from app.config import Settings
from app.services import sparse_encoder


# The driver Literal's declared values, looked up via typing so the
# test fails the *moment* the Literal changes without a corresponding
# entry below — including future additions a contributor forgets to
# wire through the encoder.
_DRIVER_VALUES: list[str] = list(
    typing.get_args(
        Settings.model_fields["vector_store_driver"].annotation,
    ),
)


# Source of truth for which driver expects which BM25 convention.
# Add a row here when a new driver lands. The encoder side is the
# ``_RAW_WEIGHT_DRIVERS`` set in sparse_encoder.py; the two must
# agree (this test enforces the agreement).
_EXPECTED: dict[str, str] = {
    # Pre-baked: doc weight = saturated TF, query weight = IDF.
    # Driver-side store does not re-compute BM25.
    "pgvector":       "pre_baked",
    "qdrant":         "pre_baked",
    "seahorse-cloud": "pre_baked",
    # Raw TF: doc weight = raw token count, query weight = 1.0.
    # Coral applies BM25 internally from (k, b, N, avgdl, df).
    "seahorse-db":      "raw_tf",
    "seahorse-db-grpc": "raw_tf",
}


def test_every_driver_has_a_declared_convention() -> None:
    """Adding a new driver enum value without updating _EXPECTED
    above is a regression — the test fails until the contributor
    states the new driver's BM25 convention out loud."""
    missing = set(_DRIVER_VALUES) - set(_EXPECTED)
    assert not missing, (
        f"vector_store_driver Literal added {missing!r} without "
        f"declaring its BM25 weight convention in this test's "
        f"_EXPECTED table. Add an entry there and update "
        f"sparse_encoder._RAW_WEIGHT_DRIVERS if the new driver "
        f"expects raw TF."
    )


@pytest.mark.parametrize("driver", _DRIVER_VALUES)
def test_encoder_flag_matches_expected_convention(
    driver: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The encoder's ``_use_raw_weights()`` must agree with the
    convention declared above. Regression for 0.8.0's silent
    miss on ``seahorse-db-grpc``."""
    expected = _EXPECTED[driver]
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_driver", driver)
    actual_raw = sparse_encoder._use_raw_weights()
    if expected == "raw_tf":
        assert actual_raw is True, (
            f"driver {driver!r} expects raw TF but encoder returned "
            f"pre-baked. Add it to sparse_encoder._RAW_WEIGHT_DRIVERS."
        )
    else:
        assert actual_raw is False, (
            f"driver {driver!r} expects pre-baked weights but encoder "
            f"returned raw. Remove it from "
            f"sparse_encoder._RAW_WEIGHT_DRIVERS or change _EXPECTED."
        )
