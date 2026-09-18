"""Top-level `/health` status aggregation (#538).

The top-level `status` was the literal `"ok"` — nothing computed it — while
the queue sections below honestly report `degraded` when they hold terminal
work. An installation with permanently abandoned intents therefore reported
`"ok"` at the top. These pin the aggregate without a live server: terminal
work degrades, pending work reconciles, anything else is ok, and a failed or
misshapen section contributes nothing rather than failing the whole response.

The rule table used to be restated here as a local copy, because both helpers
were closures on the `health` handler. A copy passes whatever the original
does, so it proved nothing about the endpoint — and the defect below lived in
exactly the half the copy was standing in for. They are module-level now and
imported, so these assertions are about the shipped code.
"""

from __future__ import annotations

from app.main import _aggregate_status, _terminal


def test_abandoned_anywhere_degrades_the_top_level():
    assert _aggregate_status([{"pending": 0, "abandoned": 3}]) == "degraded"


def test_exhausted_without_abandoned_still_degrades():
    # `exhausted` is the final claimed attempt before the rescuer stamps it
    # terminal: visible here rather than counted as ordinary retrying work.
    assert _aggregate_status([{"pending": 1, "exhausted": 1, "abandoned": 0}]) == "degraded"


def test_empty_queues_report_ok():
    assert _aggregate_status([
        {"pending": 0, "retrying": 0, "abandoned": 0},
        {"pending": 0, "abandoned": 0},
        {"upsert": {"pending": 0}},
    ]) == "ok"


def test_pending_work_reconciles_without_terminal_counts():
    assert _aggregate_status([
        {"pending": 0, "abandoned": 0},
        {"pending": 5, "retrying": 2, "abandoned": 0},
    ]) == "reconciling"


def test_retrying_alone_reconciles():
    assert _aggregate_status([{"pending": 0, "retrying": 88, "abandoned": 0}]) == "reconciling"


def test_degraded_wins_over_pending_elsewhere():
    assert _aggregate_status([
        {"pending": 40, "retrying": 10, "abandoned": 0},
        {"pending": 0, "abandoned": 1},
    ]) == "degraded"


def test_failed_section_contributes_nothing():
    # A reporter that raised becomes {"error": ...}: it must neither degrade
    # nor reconcile the top level, and must not fail the aggregation.
    assert _aggregate_status([{"error": "boom"}, {"pending": 0}]) == "ok"
    assert _aggregate_status([{"error": "boom"}, {"pending": 2}]) == "reconciling"


def test_misshapen_section_contributes_nothing():
    assert _aggregate_status([None, "ok", 42, {"pending": 0}]) == "ok"
    assert _aggregate_status([{"pending": "many", "abandoned": "none"}]) == "ok"


def test_nested_upsert_and_delete_slices_count():
    # `vector_store.backfill.upsert/.delete` join as their own entries —
    # the rule reads the slice, not the `vector_store` envelope.
    assert _aggregate_status([{"pending": 0, "abandoned": 2}]) == "degraded"
    assert _aggregate_status([{"pending": 3, "abandoned": 0}]) == "reconciling"


# ── a ledger is not a diagnosis ──────────────────────────────────────────


def test_a_section_that_keeps_a_ledger_is_read_at_its_head():
    """The counter that can never go down must not set a verdict that can't.

    `native_derived` counts every intent it ever gave up on. Save the document
    again and the new revision indexes cleanly, so nothing is missing from
    ranked search — but the entry stays, and a verdict taken from it calls the
    installation degraded for as long as it lives.
    """
    repaired = {"pending": 0, "abandoned": 2, "abandoned_at_head": 0}
    assert _terminal(repaired) == (0, 0)
    assert _aggregate_status([repaired]) == "ok"


def test_a_live_loss_still_degrades_through_the_same_key():
    live = {"pending": 0, "abandoned": 7, "abandoned_at_head": 1}
    assert _terminal(live) == (0, 1)
    assert _aggregate_status([live]) == "degraded"


def test_a_stuck_final_attempt_is_read_at_its_head_too():
    superseded = {"pending": 1, "exhausted": 1, "exhausted_at_head": 0, "abandoned": 0}
    assert _terminal(superseded) == (0, 0)
    assert _aggregate_status([superseded]) == "reconciling"


def test_a_section_without_the_suffix_keeps_reading_its_own_counters():
    """Only `native_derived` distinguishes the two populations today."""
    assert _terminal({"pending": 0, "exhausted": 2, "abandoned": 1}) == (2, 1)
    assert _aggregate_status([{"pending": 0, "abandoned": 1}]) == "degraded"


def test_the_head_key_wins_even_when_it_is_the_larger_number():
    """Preference, not a minimum — the rule is which population, not which value."""
    assert _terminal({"abandoned": 1, "abandoned_at_head": 4}) == (0, 4)


def test_a_misshapen_head_key_falls_back_to_contributing_nothing():
    # Reading the head key is still tolerant: it must not fail the response.
    assert _terminal({"abandoned": 3, "abandoned_at_head": "none"}) == (0, 0)
    assert _terminal({"abandoned": 3, "abandoned_at_head": None}) == (0, 0)
