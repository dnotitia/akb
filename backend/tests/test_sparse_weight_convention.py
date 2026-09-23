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
from app.services.sparse_shapes import SPARSE_SHAPES


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


# The convention stopped being a property of the driver alone when one driver
# gained a shape whose store computes BM25 itself. `pgvector` bakes k1/b into
# the document weights for `posting` and `arrays`; for `vchord` the index owns
# them, and sending pre-baked weights there saturates twice — the 0.7.7 bug,
# silently, as worse ranking rather than an error.
#
# Only `pgvector` reads the shape, so every other driver is declared once and
# the shape is recorded as irrelevant. Writing them out per shape anyway is
# what makes "irrelevant" a statement the test can check rather than an
# assumption nobody wrote down.
_EXPECTED_BY_SHAPE: dict[tuple[str, str], str] = {
    **{("pgvector", shape): "pre_baked" for shape in ("posting", "arrays")},
    ("pgvector", "vchord"): "raw_tf",
    **{
        (driver, shape): expected
        for driver, expected in (
            ("qdrant", "pre_baked"),
            ("seahorse-cloud", "pre_baked"),
            ("seahorse-db", "raw_tf"),
            ("seahorse-db-grpc", "raw_tf"),
        )
        for shape in SPARSE_SHAPES
    },
}


def test_every_driver_and_shape_pair_has_a_declared_convention() -> None:
    """A new shape is as capable of resurrecting 0.7.7 as a new driver was.

    `_EXPECTED` above covers drivers; this covers the pairs. Adding a shape
    without saying what it expects fails here rather than in the ranking."""
    missing = {
        (d, s) for d in _DRIVER_VALUES for s in SPARSE_SHAPES
    } - set(_EXPECTED_BY_SHAPE)
    assert not missing, (
        f"convention 이 선언되지 않은 (driver, shape) 쌍: {sorted(missing)!r}. "
        f"_EXPECTED_BY_SHAPE 에 적고, raw TF 를 기대한다면 "
        f"sparse_encoder._RAW_WEIGHT_SHAPES 도 갱신할 것."
    )


@pytest.mark.parametrize(
    "driver,shape",
    sorted(_EXPECTED_BY_SHAPE),
    ids=lambda v: str(v),
)
def test_encoder_flag_matches_the_pair(
    driver: str, shape: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _EXPECTED_BY_SHAPE[(driver, shape)]
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_driver", driver)
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_sparse_shape", shape)
    actual_raw = sparse_encoder._use_raw_weights()
    assert actual_raw is (expected == "raw_tf"), (
        f"({driver!r}, {shape!r}) 는 {expected} 를 기대하는데 encoder 는 "
        f"{'raw' if actual_raw else 'pre-baked'} 를 돌려줬다"
    )


def test_a_shape_left_over_from_another_driver_does_not_leak() -> None:
    """`vector_store_sparse_shape` is pgvector's setting. A value left in the
    config while another driver is active must not change that driver's
    convention — the shape is only consulted for the driver it belongs to."""
    for driver in _DRIVER_VALUES:
        if driver == "pgvector":
            continue
        flags = set()
        for shape in SPARSE_SHAPES:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(sparse_encoder.settings, "vector_store_driver", driver)
                mp.setattr(sparse_encoder.settings, "vector_store_sparse_shape", shape)
                flags.add(sparse_encoder._use_raw_weights())
        assert len(flags) == 1, (
            f"{driver!r} 의 규약이 pgvector 전용 설정에 따라 달라진다"
        )


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
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_sparse_shape", "posting")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("driver,shape", sorted(_EXPECTED_BY_SHAPE))
async def test_encoded_values_and_external_stats_consumption(driver, shape, monkeypatch):
    """Assert numeric output, not only the dispatch flag that selects it."""
    import math
    from unittest.mock import AsyncMock

    monkeypatch.setattr(sparse_encoder.settings, "vector_store_driver", driver)
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_sparse_shape", shape)
    monkeypatch.setattr(
        sparse_encoder, "tokenize", AsyncMock(return_value=["검색", "검색", "engine"])
    )
    vocab = {"검색": 17, "engine": 91}
    register = AsyncMock(return_value=vocab)
    lookup = AsyncMock(return_value=vocab)
    monkeypatch.setattr(sparse_encoder, "get_or_create_term_ids", register)
    monkeypatch.setattr(sparse_encoder, "lookup_term_ids", lookup)
    raw = _EXPECTED_BY_SHAPE[(driver, shape)] == "raw_tf"
    stats = AsyncMock(return_value={"total_docs": 100, "avgdl": 6, "k1": 1.5, "b": 0.75})
    df = AsyncMock(return_value={17: 2, 91: 80})
    if raw:
        stats.side_effect = AssertionError("raw encoding must not consume external stats")
        df.side_effect = AssertionError("raw encoding must not consume external df")
    monkeypatch.setattr(sparse_encoder, "load_stats", stats)
    monkeypatch.setattr(sparse_encoder, "load_df_for_terms", df)

    doc = dict(zip(*(await sparse_encoder.encode_document("document"))))
    query = dict(zip(*(await sparse_encoder.encode_query("query"))))
    if raw:
        assert doc == {17: 2.0, 91: 1.0}
        assert all(weight > 0 and weight.is_integer() for weight in doc.values())
        assert query == {17: 1.0, 91: 1.0}
        stats.assert_not_awaited()
        df.assert_not_awaited()
    else:
        norm = 1 - 0.75 + 0.75 * 3 / 6
        assert doc == pytest.approx({17: 2 * 2.5 / (2 + 1.5 * norm), 91: 2.5 / (1 + 1.5 * norm)})
        assert query == pytest.approx({17: math.log(1 + 98.5 / 2.5), 91: math.log(1 + 20.5 / 80.5)})
        assert stats.await_count == 2
        df.assert_awaited_once()
    register.assert_awaited_once()
    lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_vchord_empty_and_oov_queries_do_not_register_terms_or_read_stats(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(sparse_encoder.settings, "vector_store_driver", "pgvector")
    monkeypatch.setattr(sparse_encoder.settings, "vector_store_sparse_shape", "vchord")
    tokenize = AsyncMock(return_value=[])
    register = AsyncMock(side_effect=AssertionError("empty documents/queries must not register terms"))
    lookup = AsyncMock(return_value={})
    stats = AsyncMock(side_effect=AssertionError("raw encoding must not load stats"))
    df = AsyncMock(side_effect=AssertionError("raw encoding must not load df"))
    for name, value in (("tokenize", tokenize), ("get_or_create_term_ids", register),
                        ("lookup_term_ids", lookup), ("load_stats", stats), ("load_df_for_terms", df)):
        monkeypatch.setattr(sparse_encoder, name, value)
    assert await sparse_encoder.encode_document("") == ([], [])
    assert await sparse_encoder.encode_query("") == ([], [])
    lookup.assert_not_awaited()
    tokenize.return_value = ["unknown", "unknown"]
    assert await sparse_encoder.encode_query("unknown unknown") == ([], [])
    lookup.assert_awaited_once_with(["unknown"])
    register.assert_not_awaited()
    stats.assert_not_awaited()
    df.assert_not_awaited()
