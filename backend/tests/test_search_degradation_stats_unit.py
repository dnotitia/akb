"""Search degradation accounting (akb#612).

Nothing counted how often a search response came back `degraded`, or why, so
the question a consumer's behaviour should be chosen from — how often does the
flag fire, which cause dominates, and does the caller still get results — could
not be answered for any deployment.

These pin the three things that makes hard. The reason string is PARSED to get
the cause, so the parser and the formatter are held to each other rather than
being two independent readings of one format. The counters are in-memory on the
hot path and durable in the tables, so what is at risk between flushes is a
number the snapshot reports rather than a silent hole. And accounting sits
behind the response, so it can neither fail a search nor miss one.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import pytest

from app.models.document import SearchResponse
from app.services import search_degradation_stats as sds
from app.services import search_service as ss

pytestmark = pytest.mark.asyncio


def _response(
    *, degraded: bool = False, reason: str | None = None, results: int = 0,
) -> SearchResponse:
    return SearchResponse(
        query="x", total=results, returned=results, total_matches=results,
        degraded=degraded, degradation_reason=reason,
        results=[
            {
                "source_type": "document", "uri": f"akb://v/doc/{i}.md", "vault": "v",
                "path": f"{i}.md", "title": f"D{i}", "tags": [], "score": 1.0,
            }
            for i in range(results)
        ],
    )


class _Conn:
    """Records every statement; answers the two snapshot reads from a dict."""

    def __init__(self, totals: dict | None = None, causes: list[dict] | None = None):
        self.totals = totals
        self.causes = causes or []
        self.executed: list[tuple] = []
        self.many: list[tuple] = []
        self.fail = False

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, *args):
        if self.fail:
            raise RuntimeError("database is unreachable")
        self.executed.append((sql, args))

    async def executemany(self, sql, rows):
        if self.fail:
            raise RuntimeError("database is unreachable")
        self.many.append((sql, list(rows)))

    async def fetchrow(self, _sql, *_args):
        return self.totals

    async def fetch(self, _sql, *_args):
        return self.causes


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _install(monkeypatch, conn):
    async def get_test_pool():
        return _Pool(conn)

    monkeypatch.setattr(sds, "get_pool", get_test_pool)


@pytest.fixture(autouse=True)
def _clean():
    sds.reset()
    yield
    sds.reset()


# ── the reason string, read by the code that writes it ──────────


async def test_a_reason_round_trips_through_the_pair_that_owns_its_format():
    """The formatter and the parser are the authority on the same string, and
    this is what keeps them one decision instead of two. A free-form diagnostic
    reformatted in one place and parsed in another is how three descriptions of
    one field drifted apart in the first place (akb#604)."""
    faults = {"hydration_miss": 4, "stale_arm": 1}
    reason = ss._hydration_drop_reason(faults)

    assert reason.startswith(ss.HYDRATION_DROP_REASON_PREFIX)
    assert ss.degradation_causes(reason) == ("hydration_miss", "stale_arm")


async def test_a_retrieval_leg_is_its_own_cause():
    """A leg that raised names itself, so the reason IS the cause and passes
    through unchanged — no prefix to strip, nothing to parse."""
    assert ss.degradation_causes("sparse_encoder_degraded") == ("sparse_encoder_degraded",)
    assert ss.degradation_causes("vector_store_unavailable") == ("vector_store_unavailable",)


async def test_an_undegraded_or_unparseable_reason_names_nothing():
    """Empty rather than a guess. A malformed string is not evidence about the
    corpus, and minting a cause name from one would put unbounded keys into a
    counter that is supposed to have a closed vocabulary."""
    assert ss.degradation_causes(None) == ()
    assert ss.degradation_causes("") == ()
    assert ss.degradation_causes(ss.HYDRATION_DROP_REASON_PREFIX) == ()
    assert ss.degradation_causes("hydration_dropped:,,") == ()


# ── recording ───────────────────────────────────────────────────


async def test_every_response_counts_toward_the_denominator():
    """`observed` is "searches that happened", empty ones included. Counting
    only the interesting responses would inflate every ratio built on it — and
    the ratio is the whole point of the section."""
    sds.record(_response())
    sds.record(_response(results=3))
    sds.record(_response(degraded=True, reason="sparse_encoder_degraded", results=2))

    delta = sds._pending[datetime.now(timezone.utc).date()]
    assert delta.observed == 3
    assert delta.degraded == 1


async def test_a_degraded_response_that_kept_results_is_counted_separately():
    """The field the consumer decision turns on, and the one no existing
    surface could produce. Whether a fast keyboard surface should show partial
    results depends on how often a degraded response carries usable ones; both
    shapes exist, so the counter has to tell them apart."""
    sds.record(_response(degraded=True, reason="sparse_encoder_degraded", results=2))
    sds.record(_response(degraded=True, reason="vector_store_unavailable", results=0))

    delta = sds._pending[datetime.now(timezone.utc).date()]
    assert delta.degraded == 2
    assert delta.degraded_with_results == 1
    assert dict(delta.by_cause) == {
        "sparse_encoder_degraded": 1, "vector_store_unavailable": 1,
    }


async def test_a_multi_cause_reason_counts_under_each_cause():
    """A write race and a stale arm call for opposite responses, so a reason
    naming both contributes to both. That is why the breakdown sums to at least
    the degraded count and not exactly to it — and why `degraded` is stored
    rather than derived from the cause rows."""
    sds.record(_response(
        degraded=True, reason="hydration_dropped:hydration_miss=4,stale_arm=1",
    ))

    delta = sds._pending[datetime.now(timezone.utc).date()]
    assert delta.degraded == 1
    assert dict(delta.by_cause) == {"hydration_miss": 1, "stale_arm": 1}
    assert sum(delta.by_cause.values()) > delta.degraded


async def test_recording_can_never_fail_a_search(monkeypatch):
    """Tracking a call must not be able to fail that call. The counter sits
    behind a response that has already been assembled; an exception escaping
    here would turn a successful search into a 500 for the sake of a statistic."""
    def boom(_reason):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(sds, "degradation_causes", boom)
    sds.record(_response(degraded=True, reason="sparse_encoder_degraded"))

    # The response was still counted up to the point of failure, and nothing
    # propagated.
    assert sds.pending_depth() == 1


async def test_cause_overflow_is_folded_not_dropped():
    """The causes are a closed set well under the cap, so this never fires in
    practice — it exists so a future caller-influenced cause cannot turn a
    counter into unbounded key growth. Overflow FOLDS into one bucket: a
    counter with an unreported hole is worse than no counter."""
    for i in range(sds.MAX_CAUSES_PER_DAY + 5):
        sds.record(_response(degraded=True, reason=f"cause_{i}"))

    delta = sds._pending[datetime.now(timezone.utc).date()]
    assert len(delta.by_cause) == sds.MAX_CAUSES_PER_DAY + 1  # + the overflow bucket
    assert delta.by_cause[sds.OVERFLOW_CAUSE] == 5
    assert sum(delta.by_cause.values()) == delta.degraded


# ── flushing ────────────────────────────────────────────────────


async def test_a_flush_writes_both_tables_and_clears_the_delta(monkeypatch):
    """Durability is the whole reason this is not just a process counter:
    in-process counts reset on every deploy and deploys here are frequent."""
    conn = _Conn()
    _install(monkeypatch, conn)
    sds.record(_response())
    sds.record(_response(degraded=True, reason="hydration_dropped:hydration_miss=1", results=1))

    folded = await sds.flush_once()

    assert folded == 2
    assert sds.pending_depth() == 0
    totals_sql, totals_args = conn.executed[0]
    assert "search_degradation_daily" in totals_sql
    assert totals_args[1:] == (2, 1, 1)  # observed, degraded, degraded_with_results
    cause_sql, cause_rows = conn.many[0]
    assert "search_degradation_cause_daily" in cause_sql
    assert [(r[1], r[2]) for r in cause_rows] == [("hydration_miss", 1)]


async def test_a_flush_that_fails_gives_the_counts_back(monkeypatch):
    """A transient database blip must not put a hole in the counter that exists
    to make holes visible. The delta is claimed, and on failure merged back —
    so the next tick writes it instead of the count disappearing."""
    conn = _Conn()
    conn.fail = True
    _install(monkeypatch, conn)
    sds.record(_response())
    sds.record(_response(degraded=True, reason="sparse_encoder_degraded"))

    with pytest.raises(RuntimeError):
        await sds.flush_once()

    assert sds.pending_depth() == 2
    delta = sds._pending[datetime.now(timezone.utc).date()]
    assert delta.degraded == 1
    assert dict(delta.by_cause) == {"sparse_encoder_degraded": 1}

    conn.fail = False
    assert await sds.flush_once() == 2
    assert sds.pending_depth() == 0


async def test_an_empty_flush_does_no_work(monkeypatch):
    """The flusher ticks whether or not the deployment served a search; an
    idle tick must not open a connection."""
    conn = _Conn()
    _install(monkeypatch, conn)

    assert await sds.flush_once() == 0
    assert conn.executed == []


# ── the snapshot ────────────────────────────────────────────────


async def test_the_snapshot_adds_the_unflushed_delta_to_the_stored_rows(monkeypatch):
    """`/health` answers a question asked seconds after a search, not one flush
    interval later. The stored half already covers every pod; the delta is this
    one's, which is why `pending_flush` is reported beside the counts instead of
    being silently baked into them."""
    conn = _Conn(
        totals={"observed": 100, "degraded": 7, "degraded_with_results": 4},
        causes=[{"cause": "hydration_miss", "responses": 5},
                {"cause": "sparse_encoder_degraded", "responses": 2}],
    )
    _install(monkeypatch, conn)
    sds.record(_response())
    sds.record(_response(degraded=True, reason="hydration_dropped:hydration_miss=1", results=2))

    snap = await sds.snapshot()

    assert snap["observed"] == 102
    assert snap["degraded"] == 8
    assert snap["degraded_with_results"] == 5
    assert snap["by_cause"] == {"hydration_miss": 6, "sparse_encoder_degraded": 2}
    assert snap["pending_flush"] == 2
    assert snap["day"] == datetime.now(timezone.utc).date().isoformat()


async def test_the_snapshot_reads_zero_before_the_first_search_of_the_day(monkeypatch):
    """A day with no row yet is zeros, not an error and not an absent section:
    a reader compares the numbers, so they have to exist."""
    _install(monkeypatch, _Conn(totals=None, causes=[]))

    snap = await sds.snapshot()

    assert snap["observed"] == 0
    assert snap["degraded"] == 0
    assert snap["degraded_with_results"] == 0
    assert snap["by_cause"] == {}
    assert snap["pending_flush"] == 0


async def test_the_section_reports_counts_and_no_verdict(monkeypatch):
    """Shaped like the neighbouring `/health` sections: numbers, and the reader
    judges. It deliberately does not join `_aggregate_status` either — a search
    degraded by a write race is not queue work left undone, and letting it move
    the top-level verdict would make an ordinary delete look like an outage."""
    _install(monkeypatch, _Conn(totals=None, causes=[]))

    snap = await sds.snapshot()

    assert "status" not in snap
    assert "healthy" not in snap
    assert not {"pending", "retrying", "exhausted", "abandoned"} & set(snap)

    from app.main import _aggregate_status
    assert _aggregate_status([snap]) == "ok"


async def test_counts_are_kept_per_day(monkeypatch):
    """A process that crosses midnight before a flush holds two days, and each
    is written to its own row. Folding them into whichever day the flush
    happened to run on would move counts between the days being compared."""
    conn = _Conn()
    _install(monkeypatch, conn)
    sds._pending[date(2026, 9, 17)] = sds._Delta(observed=3, degraded=1)
    sds._pending[date(2026, 9, 18)] = sds._Delta(observed=5, degraded=0)

    assert await sds.flush_once() == 8
    assert sorted(args[0] for _sql, args in conn.executed) == [
        date(2026, 9, 17), date(2026, 9, 18),
    ]


# ── the chokepoint ──────────────────────────────────────────────


async def test_search_is_wrapped_so_no_return_site_can_escape_counting():
    """`search` has four return sites and adding a fifth is an ordinary edit,
    so the counter is a decorator rather than a call before each `return`. A
    chokepoint that has to be remembered is one that will be forgotten, and a
    denominator with a hole in it understates every ratio silently.

    The end-to-end proof — a real search through the service producing exactly
    one observation — lives beside the search harness in
    `test_search_archive_scope_vault_path_unit.py`."""
    assert hasattr(ss.SearchService.search, "__wrapped__")
    assert ss.SearchService.search.__wrapped__ is not ss.SearchService.search
