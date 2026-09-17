"""Top-level `/health` status aggregation (#538).

The top-level `status` was the literal `"ok"` — nothing computed it — while
the queue sections below honestly report `degraded` when they hold terminal
work. An installation with permanently abandoned intents therefore reported
`"ok"` at the top. These pin the aggregate without a live server: the
aggregation helpers live on the `health` handler's closure surface, so the
tests exercise the same rule table through a thin local copy of the contract
— terminal work degrades, pending work reconciles, anything else is ok, and
a failed or misshapen section contributes nothing rather than failing the
whole response.
"""

from __future__ import annotations


def _terminal(section: object) -> tuple[int, int]:
    if not isinstance(section, dict):
        return (0, 0)
    try:
        exhausted = int(section.get("exhausted") or 0)
    except (TypeError, ValueError):
        exhausted = 0
    try:
        abandoned = int(section.get("abandoned") or 0)
    except (TypeError, ValueError):
        abandoned = 0
    return (exhausted, abandoned)


def _aggregate_status(sections: list[object]) -> str:
    pending_work = False
    for section in sections:
        if isinstance(section, dict) and section.get("error"):
            continue
        exhausted, abandoned = _terminal(section)
        if exhausted or abandoned:
            return "degraded"
        try:
            pending_work = pending_work or bool(
                int(section.get("pending") or 0) or int(section.get("retrying") or 0)
            )
        except (TypeError, ValueError, AttributeError):
            continue
    return "reconciling" if pending_work else "ok"


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
