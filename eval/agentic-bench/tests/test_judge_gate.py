"""The gate and the cost tradeoff are arithmetic, so they are tested as
arithmetic.

The bench exists to answer one question — is a cheaper response still a
correct one — and the aggregator turns that into a pass/fail against an
accuracy floor plus a readout of what the change saves against what its
wrong answers cost to redirect. Getting the sign or the scaling wrong there
would make a payload regression look like a win, so each piece is pinned.
"""

from __future__ import annotations

import math

import pytest

from src.judge import (
    DEFAULT_ACCURACY_FLOOR,
    TRADEOFF_FLOORS,
    break_even_accuracy,
    gate_verdict,
    payload_per_call,
    tokens_per_correct_answer,
    tradeoff_row,
)


# ── the gate ────────────────────────────────────────────────────────


def test_the_default_floor_is_ninety_five_percent():
    assert DEFAULT_ACCURACY_FLOOR == 0.95
    assert TRADEOFF_FLOORS == (0.90, 0.80)


@pytest.mark.parametrize(
    "pass_rate,floor,expected",
    [
        (0.96, 0.95, "PASS"),
        (0.95, 0.95, "PASS"),   # the floor is acceptable, not a strict minimum
        (0.9499, 0.95, "FAIL"),
        (0.94, 0.90, "PASS"),
        (0.79, 0.80, "FAIL"),
        (0.0, 0.95, "FAIL"),
        (1.0, 0.95, "PASS"),
    ],
)
def test_gate_verdict(pass_rate, floor, expected):
    assert gate_verdict(pass_rate, floor) == expected


# ── tokens per correct answer ───────────────────────────────────────


def test_tokens_per_correct_answer_divides_by_passes_not_questions():
    # 40 questions, 400_000 tokens, 20 of them right: the mean per question
    # (10_000) flatters an arm that answers cheaply and wrongly.
    assert tokens_per_correct_answer(400_000, 20) == 20_000


def test_an_arm_with_no_correct_answer_has_no_finite_cost():
    assert math.isinf(tokens_per_correct_answer(400_000, 0))


# ── payload per call ────────────────────────────────────────────────


def test_payload_per_call_reports_calls_and_both_denominators():
    stats = payload_per_call([1000, 2000, 3000, 6000], questions=2)

    assert stats["calls"] == 4
    assert stats["total_chars"] == 12_000
    assert stats["chars_per_call"] == 3000
    assert stats["chars_per_question"] == 6000


def test_payload_per_call_survives_an_arm_that_made_no_call():
    stats = payload_per_call([], questions=0)

    assert stats["calls"] == 0
    assert stats["chars_per_call"] == 0.0
    assert stats["chars_per_question"] == 0.0


# ── the tradeoff ────────────────────────────────────────────────────


def test_expected_redirect_cost_is_one_minus_p_times_d():
    row = tradeoff_row(
        pass_rate=0.90,
        floor=0.95,
        redirect_cost=8000,
        saving_per_call=0,
        calls_per_question=0,
    )

    assert row["expected_redirect_tokens"] == pytest.approx(800.0)
    assert row["verdict"] == "FAIL"


def test_the_saving_is_scaled_by_the_calls_a_question_actually_makes():
    row = tradeoff_row(
        pass_rate=0.96,
        floor=0.95,
        redirect_cost=0,
        saving_per_call=250,
        calls_per_question=3.2,
    )

    assert row["saving_tokens"] == pytest.approx(800.0)
    assert row["verdict"] == "PASS"


def test_net_is_the_saving_less_the_expected_redirect():
    row = tradeoff_row(
        pass_rate=0.90,
        floor=0.90,
        redirect_cost=8000,
        saving_per_call=250,
        calls_per_question=4,
    )

    # saving 1000/question, expected redirect 800/question
    assert row["saving_tokens"] == pytest.approx(1000.0)
    assert row["expected_redirect_tokens"] == pytest.approx(800.0)
    assert row["net_tokens"] == pytest.approx(200.0)


def test_a_change_that_saves_less_than_it_costs_shows_a_negative_net():
    row = tradeoff_row(
        pass_rate=0.80,
        floor=0.95,
        redirect_cost=8000,
        saving_per_call=100,
        calls_per_question=3,
    )

    assert row["expected_redirect_tokens"] == pytest.approx(1600.0)
    assert row["saving_tokens"] == pytest.approx(300.0)
    assert row["net_tokens"] == pytest.approx(-1300.0)
    assert row["verdict"] == "FAIL"


def test_the_same_measurement_reads_differently_at_each_floor():
    verdicts = {
        floor: tradeoff_row(
            pass_rate=0.88,
            floor=floor,
            redirect_cost=8000,
            saving_per_call=250,
            calls_per_question=3.2,
        )["verdict"]
        for floor in (0.95, 0.90, 0.80)
    }

    assert verdicts == {0.95: "FAIL", 0.90: "FAIL", 0.80: "PASS"}


# ── break-even ──────────────────────────────────────────────────────


def test_break_even_is_the_accuracy_at_which_the_saving_pays_for_itself():
    # saving 800/question against D = 8000 → p* = 1 - 0.1 = 0.90
    assert break_even_accuracy(redirect_cost=8000, saving=800) == pytest.approx(0.90)


def test_at_break_even_the_net_is_zero():
    p_star = break_even_accuracy(redirect_cost=8000, saving=800)
    row = tradeoff_row(
        pass_rate=p_star,
        floor=0.95,
        redirect_cost=8000,
        saving_per_call=800,
        calls_per_question=1,
    )

    assert row["net_tokens"] == pytest.approx(0.0)


def test_without_a_redirect_cost_there_is_nothing_to_trade_against():
    assert break_even_accuracy(redirect_cost=0, saving=800) is None
    row = tradeoff_row(
        pass_rate=0.9,
        floor=0.95,
        redirect_cost=0,
        saving_per_call=800,
        calls_per_question=1,
    )
    assert row["break_even_accuracy"] is None
    assert row["expected_redirect_tokens"] == 0.0
