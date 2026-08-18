"""Unit tests for MCP tool-usage tracking (`app.services.tool_usage`).

This sink exists because AKB cannot currently answer "which MCP tool is
actually used" — `events` records domain verbs on writes only (at least 29
of 43 tools can never appear) and `audit_log` writes a hash-chained file
that cannot be grouped. See
`docs/design/proposal/2026-07-28-mcp-tool-usage-tracking/README.md`.

The properties under test are the ones whose regression would be silent:

* **Non-blocking.** `record()` runs on the single event loop. This codebase's
  dominant outage class is loop stalls (503). `server.py:1431` states the
  contract for the sibling audit sink — "No disk I/O or lock on the event
  loop". A `record()` that ever awaits or touches the DB reintroduces it.
* **No silent loss.** When the queue overflows we drop, but the drop must be
  counted and observable. Silent narrowing/loss is a recurring defect in this
  codebase (search scope, MCP session forking, repair laundering).
* **Never raises.** Usage tracking must not be able to fail a tool call.
* **A call is counted once.** Both exits of the dispatch chokepoint must not
  record the same invocation.

The rollup/purge correctness properties — exactly-once claiming under
concurrent writers, late arrivals, purge never outrunning the aggregate,
migration idempotency — are NOT provable against a fake connection, because
they are properties of the SQL and of Postgres' visibility rules. They live in
`test_tool_usage_e2e.py`, which runs against a live database. Asserting them
here by matching strings in `_ROLLUP_SQL` would look like coverage and prove
nothing.

No DB and no event loop I/O: the pool is faked inline (the pattern used by
`test_activity_git_offload_unit.py`). Runs in `pytest -k 'not _e2e'`.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import logging.handlers
import pathlib
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services import tool_usage


class _User:
    user_id = "00000000-0000-0000-0000-000000000001"
    username = "alice"


@pytest.fixture
def enabled(monkeypatch):
    """Enable tracking and start from an empty queue."""
    monkeypatch.setattr(settings.tool_usage, "enabled", True)
    tool_usage.reset()
    yield
    tool_usage.reset()


# ── The non-blocking contract ───────────────────────────────────


def test_record_is_synchronous_and_only_enqueues(enabled):
    """`record()` must be a plain function, not a coroutine — an `async def`
    here would put an await point (and eventually DB I/O) on the request path,
    which is exactly the loop-stall class that causes 503s in this service."""
    assert not inspect.iscoroutinefunction(tool_usage.record)

    tool_usage.record("akb_put", {"vault": "v"}, _User(), {"ok": True})

    assert tool_usage.queue_depth() == 1
    assert tool_usage.dropped_count() == 0


def test_record_does_not_touch_the_pool(enabled, monkeypatch):
    """The recording path must never acquire a DB connection. A regression that
    inlines the INSERT would block the loop on pool contention."""
    def _boom():
        raise AssertionError("record() acquired a DB pool on the request path")

    monkeypatch.setattr(tool_usage, "get_pool", _boom)
    tool_usage.record("akb_search", {"vault": "v"}, _User(), {"results": []})
    assert tool_usage.queue_depth() == 1


# ── No silent loss ──────────────────────────────────────────────


def test_queue_overflow_drops_oldest_and_counts_it(enabled, monkeypatch):
    """Under a flood the queue is bounded, but the loss must be *observable*.
    A bare `deque(maxlen=N)` silently evicts — the count is what turns this
    into a reportable degradation rather than a hole in the data."""
    monkeypatch.setattr(settings.tool_usage, "queue_max", 3)
    tool_usage.reset()

    for i in range(5):
        tool_usage.record(f"akb_t{i}", {}, _User(), {"ok": True})

    assert tool_usage.queue_depth() == 3
    assert tool_usage.dropped_count() == 2, "dropped records must be counted"
    # Oldest evicted → the survivors are the LAST three.
    assert [r.tool for r in tool_usage.drain(10)] == ["akb_t2", "akb_t3", "akb_t4"]


def test_record_never_raises(enabled):
    """A malformed argument must not fail the tool call it is describing."""
    class _Exploding:
        @property
        def username(self):
            raise RuntimeError("boom")

    tool_usage.record("akb_put", {"vault": "v"}, _Exploding(), {"ok": True})
    tool_usage.record("akb_put", None, None, None)  # type: ignore[arg-type]


def test_disabled_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings.tool_usage, "enabled", False)
    tool_usage.reset()
    tool_usage.record("akb_put", {"vault": "v"}, _User(), {"ok": True})
    assert tool_usage.queue_depth() == 0


# ── What gets recorded ──────────────────────────────────────────


def test_error_outcome_and_code_are_captured(enabled):
    tool_usage.record(
        "akb_get", {"vault": "v", "uri": "akb://v/doc/x.md"}, _User(),
        {"error": "not found", "code": "NOT_FOUND"},
    )
    row = tool_usage.drain(1)[0]
    assert row.outcome == "error"
    assert row.code == "NOT_FOUND"


def test_raw_args_are_not_stored(enabled):
    """Args carry document bodies, search queries and SQL. Only the vault is
    extracted — the same "honest, lossy" choice `audit_log` made for `target`."""
    secretish = {"vault": "v", "content": "PATIENT RECORD", "query": "salary"}
    tool_usage.record("akb_put", secretish, _User(), {"ok": True})
    row = tool_usage.drain(1)[0]
    assert row.vault == "v"
    flat = repr(row)
    assert "PATIENT RECORD" not in flat and "salary" not in flat


@pytest.mark.parametrize("args,expected", [
    ({"vault": "v"}, "v"),
    ({"uri": "akb://eng/coll/specs/doc/api.md"}, "eng"),
    ({"parent": "akb://ops/coll/runbooks"}, "ops"),
    ({"source": "akb://a/doc/x.md", "target": "akb://b/doc/y.md"}, "a"),
    # The URI wins, because that is what the handler acts on.
    ({"vault": "ignored", "uri": "akb://other/doc/x.md"}, "other"),
    ({"query": "no vault anywhere"}, None),
    ({"uri": "not-a-uri"}, None),
    # Shapes the canonical grammar rejects must not be guessed at.
    ({"uri": "akb://eng/not-a-resource"}, None),
    ({"uri": "akb://eng//doc/x.md"}, None),
])
def test_vault_is_derived_from_uris_too(enabled, args, expected):
    """20 of the 43 tools take no `vault` argument at all — `akb_get`,
    `akb_update`, `akb_edit`, `akb_delete`, `akb_move`, `akb_link` and friends
    address resources by `akb://` URI, because the canonical API deliberately
    dropped the redundant vault parameter. Reading only `args.vault` would
    leave the per-vault dimension NULL on most rows.

    Precedence matters as much as coverage: `_handle_browse`, `_handle_graph`
    and `_resolve_parent` all treat the URI as authoritative and ignore a
    `vault` passed beside it, so recording `vault` first would attribute the
    call to a vault it never touched."""
    tool_usage.record("akb_x", args, _User(), {"ok": True})
    assert tool_usage.drain(1)[0].vault == expected


@pytest.mark.parametrize("tool,args,expected", [
    # `_handle_sql` reads `vaults or [vault]` — the ARRAY wins.
    ("akb_sql", {"vaults": ["eng"], "vault": "ignored"}, "eng"),
    # …and a genuinely multi-vault statement has no single target. Recording
    # the first element would present one arbitrarily-ordered vault as *the*
    # vault the call touched.
    ("akb_sql", {"vaults": ["eng", "ops"]}, None),
    ("akb_sql", {"vault": "solo"}, "solo"),
    # `akb_publish(resource_type="table_query")` takes the scalar `vault` and
    # ignores a `uri`, even though the schema accepts both.
    ("akb_publish", {"resource_type": "table_query", "vault": "v",
                     "uri": "akb://other/doc/x.md"}, "v"),
    # …but the document form resolves from the URI.
    ("akb_publish", {"resource_type": "document", "uri": "akb://d/doc/x.md"}, "d"),
])
def test_vault_attribution_follows_each_tool(enabled, tool, args, expected):
    """One global precedence rule is wrong: the handlers disagree with each
    other, so tracking has to follow whichever one will actually run. Getting
    this wrong writes a vault the call never touched, and nothing cross-checks
    the two."""
    tool_usage.record(tool, args, _User(), {"ok": True})
    assert tool_usage.drain(1)[0].vault == expected


def test_absurd_uri_argument_is_not_parsed(enabled, monkeypatch):
    """URI parsing is regex work on the request path. Four 1 MB arguments were
    measured at ~45ms of event-loop time — the stall class this service dies
    of. Anything past a plausible URI length is rejected without parsing.

    Asserted by observing that the parser is never called, rather than by a
    wall-clock bound: scheduler preemption alone would make a timing assertion
    flaky on a loaded runner."""
    seen: list[str] = []
    monkeypatch.setattr(
        tool_usage, "vault_of", lambda u: (seen.append(u), None)[1],
    )
    args = {k: "akb://" + "x" * 1_000_000 for k in ("uri", "parent", "source", "target")}

    tool_usage.record("akb_x", args, _User(), {"ok": True})

    assert tool_usage.drain(1)[0].vault is None
    assert seen == [], "oversized arguments must never reach the URI parser"


def test_unpublish_by_slug_records_no_vault(enabled):
    """`_handle_unpublish` resolves the vault from the publication row when a
    `slug` is given and never looks at `uri`, though the schema accepts both.
    The real vault is only knowable by a DB lookup this path must not do, so
    NULL is the honest answer — recording the URI's vault would name one the
    call may not have touched."""
    tool_usage.record(
        "akb_unpublish", {"slug": "abc123", "uri": "akb://other/doc/x.md"},
        _User(), {"deleted": 1},
    )
    assert tool_usage.drain(1)[0].vault is None


def test_session_and_duration_are_recorded(enabled):
    """`session_id` is the correlation key for agent-behaviour analysis and
    `duration_ms` is what makes the ops-debugging purpose answerable."""
    tool_usage.record(
        "akb_search", {"vault": "v"}, _User(), {"results": []},
        session_id="sess-1", duration_ms=42, is_write=False,
    )
    row = tool_usage.drain(1)[0]
    assert row.session_id == "sess-1"
    assert row.duration_ms == 42
    assert row.is_write is False


# ── Client-controlled input must not reach the sink unbounded ───


def test_oversized_strings_are_clipped(enabled):
    """`tool` is the raw JSON-RPC method name and `vault` is a raw argument —
    both attacker-chosen, and both land in a table on the volume that also
    holds the source-of-truth Postgres.

    `tool` additionally becomes part of `tool_usage_daily`'s btree PRIMARY KEY,
    where a value over ~2704 bytes raises `index row size exceeds btree
    maximum`. Because the claim and the aggregate are one statement, that
    rolls the whole rollup back and the same row is re-selected every tick
    forever — folding stops, and with it purge, so retention never fires again.

    The sibling audit sink already clips its lifted argument at
    `_TARGET_MAX`; this one must too."""
    tool_usage.record("akb_" + "x" * 5000, {"vault": "v" * 5000}, _User(), {"ok": True})
    row = tool_usage.drain(1)[0]

    assert len(row.tool) == tool_usage._STR_MAX
    assert len(row.vault) == tool_usage._STR_MAX
    assert len(row.tool.rsplit("\u2026", 1)[1]) == tool_usage._DIGEST
    assert row.tool.startswith("akb_x")
    assert "…" in row.tool, "truncation must be visible, not silent"

    # Two distinct overlong tools must NOT collapse into one aggregate row —
    # `tool` is part of `tool_usage_daily`'s PRIMARY KEY, so a bare ellipsis
    # would silently sum unrelated traffic.
    tool_usage.record("akb_" + "x" * 400 + "A" + "y" * 5000, {}, _User(), {"ok": True})
    tool_usage.record("akb_" + "x" * 400 + "B" + "y" * 5000, {}, _User(), {"ok": True})
    a, b = tool_usage.drain(2)
    assert a.tool != b.tool, "truncated values must stay distinguishable"

    # Note, not asserted: distinguishing power stops at `_SCAN_MAX`, because
    # hashing the whole of an attacker-sized value is linear work on the
    # request path (58ms for 100MB). Values differing only past that boundary
    # collide. That is the accepted price of a flat-cost sanitiser, not a
    # property worth pinning — raising the bound would be an improvement, and
    # an equality assertion here would fail it.


def test_nul_bytes_are_stripped(enabled):
    """PostgreSQL TEXT cannot store NUL. Un-sanitised, a single
    `{"vault": "\\x00"}` fails the batched INSERT — and because a failed batch
    goes back to the HEAD of the deque, the very same rows are re-drained and
    re-fail every tick, permanently. One call kills the sink until restart."""
    tool_usage.record("akb_put", {"vault": "a\x00b"}, _User(), {"ok": True})
    row = tool_usage.drain(1)[0]
    assert "\x00" not in (row.vault or "")
    assert row.vault == "ab"


@pytest.mark.parametrize("field,args,kwargs", [
    ("session_id", {}, {"session_id": "s" * 5000}),
    ("code", {}, {}),
])
def test_every_stored_string_is_bounded(enabled, field, args, kwargs):
    """Not just the two obvious ones — every string column is caller-influenced
    (`code` comes from a handler's error envelope) and none may be unbounded."""
    result = {"error": "x", "code": "C" * 5000} if field == "code" else {"ok": True}
    tool_usage.record("akb_put", args, _User(), result, **kwargs)
    value = getattr(tool_usage.drain(1)[0], field)
    assert value is None or len(value) <= tool_usage._STR_MAX


def test_unstorable_batch_is_dropped_but_a_transient_outage_is_not(enabled, monkeypatch):
    """The two failures must be told apart by KIND, not by a retry count.

    A count-based rule loses data on ordinary infrastructure events: at the
    5s cadence an ordinary 10-15s PostgreSQL restart exceeds three attempts
    and the batch is discarded even though the queue had ample room. And a
    global counter is not tied to a batch — an unrelated batch can inherit
    another's failures and be dropped on its own first error.

    Class-22 (`DataError`) is a property of the rows and will never succeed;
    everything else is transient and must cost nothing."""
    from asyncpg.exceptions import CharacterNotInRepertoireError

    calls = {"n": 0}

    async def _transient():
        calls["n"] += 1
        raise ConnectionResetError("failover")

    tool_usage.record("akb_put", {"vault": "v"}, _User(), {"ok": True})
    monkeypatch.setattr(tool_usage, "get_pool", _transient)
    for _ in range(10):                       # far more than any retry budget
        assert asyncio.run(tool_usage.flush_once()) == 0
    assert tool_usage.queue_depth() == 1, "a transient outage must not lose rows"
    assert tool_usage.dropped_count() == 0
    assert calls["n"] == 10, "each tick must genuinely retry"

    # One poison row among valid siblings: `executemany` is atomic, so failing
    # the call would discard up to `flush_batch` good records. Bisecting must
    # isolate the single offender.
    tool_usage.reset()
    for i in range(8):
        tool_usage.record(f"akb_t{i}", {"vault": "v"}, _User(), {"ok": True})

    box: list = []

    class _Conn2:
        def transaction(self):
            return _Tx()

        async def executemany(self, sql, rows):
            box.append(len(rows))
            if any(r.tool == "akb_t5" for r in rows):
                raise CharacterNotInRepertoireError("invalid byte sequence")

    _fake_pool_conn(monkeypatch, _Conn2())
    written = asyncio.run(tool_usage.flush_once())

    assert written == 7, f"only the poison row may be lost, got {written} written"
    assert tool_usage.dropped_count() == 1, "and exactly one loss counted"
    assert max(box) == 8 and min(box) == 1, "the batch must have been bisected"


def test_isolation_work_is_budgeted():
    """Bisecting costs 1 statement for a clean batch and ~17 to isolate one bad
    row — but 2n-1 when the whole batch is unstorable: 999 round trips for 500
    rows, seconds of a held connection, repeated every tick for as long as the
    cause lasts. Past a budget the remainder is dropped wholesale.

    The budget must not cost precision in the case that actually happens (a
    single bad row among many good ones)."""
    from asyncpg.exceptions import CharacterNotInRepertoireError

    now = datetime.now(timezone.utc)

    def _row(i, poison):
        return tool_usage._Row(
            now, f"t{i}", "u", "a", None, "bad" if poison else "ok",
            "ok", None, 1, False,
        )

    async def _measure(pred):
        stmts = {"n": 0}

        class _C:
            def transaction(self):
                return _Tx()

            async def executemany(self, sql, rows):
                stmts["n"] += 1
                if any(r.vault == "bad" for r in rows):
                    raise CharacterNotInRepertoireError("x")

        batch = [_row(i, pred(i)) for i in range(500)]
        written, dropped = await tool_usage._insert_isolating_poison(_C(), batch)
        return written, dropped, stmts["n"]

    clean = asyncio.run(_measure(lambda i: False))
    assert clean == (500, 0, 1), f"a clean batch must cost one statement, got {clean}"

    one_bad = asyncio.run(_measure(lambda i: i == 250))
    assert one_bad[0] == 499 and one_bad[1] == 1, (
        f"a single bad row must cost only itself, got {one_bad}"
    )

    all_bad = asyncio.run(_measure(lambda i: True))
    assert all_bad[2] <= tool_usage._BISECT_BUDGET, (
        f"isolation must be bounded, spent {all_bad[2]} statements"
    )
    assert all_bad[1] == 500, "and every unstorable row still accounted for"


# ── Flush ───────────────────────────────────────────────────────


class _Tx:
    """Stand-in for `conn.transaction()`.

    The real insert path wraps the traversal in one transaction with a
    SAVEPOINT per probe, so a double that lacks this would exercise a code
    path production never takes — the test-double drift that let two earlier
    defects through.
    """
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, box):
        self.box = box

    def transaction(self):
        return _Tx()

    async def executemany(self, sql, rows):
        self.box.append((sql, list(rows)))

    async def execute(self, sql, *args):
        self.box.append((sql, list(args)))

    async def fetchval(self, sql, *args):
        return self.box_fetchval if hasattr(self, "box_fetchval") else None


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _fake_pool_conn(monkeypatch, conn):
    async def _get_pool():
        return _Pool(conn)

    monkeypatch.setattr(tool_usage, "get_pool", _get_pool)


def _fake_pool(monkeypatch, box):
    conn = _Conn(box)

    async def _get_pool():
        return _Pool(conn)

    monkeypatch.setattr(tool_usage, "get_pool", _get_pool)
    return conn


def test_flush_batches_and_empties_the_queue(enabled, monkeypatch):
    box: list = []
    _fake_pool(monkeypatch, box)
    for i in range(4):
        tool_usage.record(f"akb_t{i}", {"vault": "v"}, _User(), {"ok": True})

    n = asyncio.run(tool_usage.flush_once())

    assert n == 4
    assert tool_usage.queue_depth() == 0
    assert len(box) == 1, "one batched executemany, not one INSERT per row"
    sql, rows = box[0]
    assert "INSERT INTO tool_calls" in sql
    assert len(rows) == 4


def test_flush_on_empty_queue_does_no_io(enabled, monkeypatch):
    box: list = []
    _fake_pool(monkeypatch, box)
    assert asyncio.run(tool_usage.flush_once()) == 0
    assert box == [], "an idle flusher must not touch the DB every tick"


def test_failed_flush_requeues_instead_of_losing_the_batch(enabled, monkeypatch):
    """A transient DB error must delay records, not delete them. The batch is
    already drained at that point, so without an explicit requeue every blip
    silently punches a hole in the usage data."""
    async def _boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr(tool_usage, "get_pool", _boom)
    tool_usage.record("akb_put", {"vault": "v"}, _User(), {"ok": True})

    assert asyncio.run(tool_usage.flush_once()) == 0
    assert tool_usage.queue_depth() == 1, "the batch must return to the queue"


def test_stop_drains_the_queue(enabled, monkeypatch):
    """A rollout sends SIGTERM and `terminationGracePeriodSeconds` is unset
    (k8s default 30s). Anything still queued exists only in memory, so without
    a final drain on shutdown every deploy silently loses the tail."""
    box: list = []
    _fake_pool(monkeypatch, box)
    tool_usage.record("akb_put", {"vault": "v"}, _User(), {"ok": True})

    asyncio.run(tool_usage.stop())

    assert tool_usage.queue_depth() == 0
    assert any("INSERT INTO tool_calls" in sql for sql, _ in box), (
        "stop() must flush what is still queued"
    )


def test_flusher_does_not_log_on_every_tick():
    """`BackfillRunner` logs "processed N items" on every non-empty tick. That
    is useful for workers whose queue is usually empty (embed, delete, backfill)
    — but this one has work whenever the service has traffic, so at a 5s poll it
    emits on the order of 10k INFO lines a day and buries the messages that
    actually matter (queue overflow, flush failure).

    Verified against the local stack before this flag existed:
    `INFO akb.tool_usage_flusher: tool_usage_flusher processed 6 items`."""
    from app.services._backfill import BackfillRunner

    async def _spin(runner):
        runner.start()
        await asyncio.sleep(0.02)
        await runner.stop()

    async def _did_work():
        return 1                       # always non-empty → takes the log branch

    def _run(**kw) -> list[str]:
        handler = logging.handlers.MemoryHandler(capacity=10_000)
        log = logging.getLogger("akb.t_probe")
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            asyncio.run(_spin(BackfillRunner("t_probe", _did_work, idle_secs=1, **kw)))
            return [r.getMessage() for r in handler.buffer]
        finally:
            log.removeHandler(handler)

    assert any("processed" in m for m in _run()), (
        "the default must keep the per-tick progress line"
    )
    assert not any("processed" in m for m in _run(log_progress=False)), (
        "log_progress=False must silence the per-tick line"
    )

    assert tool_usage._flusher._log_progress is False, (
        "the usage flusher must opt out — it has work on every tick"
    )


def test_cancelled_flush_returns_its_batch(enabled, monkeypatch):
    """`CancelledError` is a `BaseException`, so `except Exception` does not
    see it. When the shutdown deadline fires mid-INSERT, the batch has already
    been popped by `drain()` — without an explicit handler it exists nowhere:
    not inserted, not requeued, not counted, and `queue_depth()` reads 0 so
    even the "drain incomplete" warning stays silent.

    Reproduced by the reviewer as `depth=0, dropped=0` after a row had been
    drained."""
    async def _hang():
        await asyncio.sleep(10)

    monkeypatch.setattr(tool_usage, "get_pool", _hang)
    tool_usage.record("akb_put", {"vault": "v"}, _User(), {"ok": True})

    async def _drive():
        task = asyncio.ensure_future(tool_usage.flush_once())
        await asyncio.sleep(0)                       # let it drain and block
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())
    assert tool_usage.queue_depth() == 1, (
        "a cancelled flush must put its batch back, not vanish with it"
    )


def test_runner_stop_waits_for_the_shielded_iteration(enabled):
    """`BackfillRunner` shields `_process_once()` so an in-flight commit is not
    torn in half. But on timeout `stop()` cancelled only the *wrapper* — the
    shielded work carried on detached while `stop()` cleared its task handles
    and returned.

    `tool_usage.stop()` then drained believing it was the sole claimant, which
    is exactly the race the reordering was meant to remove: the reviewer saw
    `depth=0` right after `stop()`, then `depth=1` when the detached flush
    failed and requeued afterwards.

    The contract is not "the iteration always finishes" — a budget that expires
    must still cancel. It is that **nothing is left running once `stop()`
    returns**, so a caller draining the shared queue afterwards cannot be
    surprised by a late write.
    """
    from app.services._backfill import BackfillRunner

    finished: list[str] = []

    async def _slow():
        await asyncio.sleep(0.20)
        finished.append("done")
        return 0

    async def _drive():
        runner = BackfillRunner("t_shield", _slow, idle_secs=1)
        runner.start()
        await asyncio.sleep(0.02)                    # let the iteration begin
        await runner.stop(timeout=0.03)              # expires mid-iteration
        at_return = list(finished)
        await asyncio.sleep(0.30)                    # far longer than the work
        return runner, at_return, list(finished)

    runner, at_return, later = asyncio.run(_drive())

    assert not runner._inflight, "no in-flight work may outlive stop()"
    assert later == at_return, (
        "the shielded iteration ran on detached after stop() returned — the "
        "caller's 'I am the only claimant' assumption is false"
    )


def test_stop_reports_whether_it_actually_quiesced(enabled):
    """A bounded `stop()` deliberately returns with work still running. A
    caller that then assumes sole ownership of the shared queue is wrong — the
    abandoned flush can requeue after the final drain and after the
    queue-depth check, so an empty queue reads as a clean shutdown while the
    tail is lost at process exit.

    The outcome therefore has to be reported, not implied. The older test uses
    a cancellation-cooperative coroutine and cannot see this."""
    from app.services._backfill import BackfillRunner

    async def _stubborn():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0.15)        # delays acknowledging cancellation
            raise
        return 0

    async def _cooperative():
        await asyncio.sleep(0.01)
        return 0

    async def _drive(fn, budget):
        r = BackfillRunner(f"t_q_{id(fn)}", fn, idle_secs=1)
        r.start()
        await asyncio.sleep(0.02)
        ok = await r.stop(timeout=budget)
        # Sampled here, not after the loop closes — `asyncio.run` finishes
        # pending tasks on the way out, so an abandoned one looks done by then.
        return ok, r.abandoned()

    quiesced_bad, abandoned = asyncio.run(_drive(_stubborn, 0.03))
    assert quiesced_bad is False, (
        "a stop that abandoned live work must not report success"
    )
    assert abandoned == 1, "and the surviving iteration must stay visible"

    quiesced_ok, none_left = asyncio.run(_drive(_cooperative, 1.0))
    assert quiesced_ok is True, "a clean stop must report success"
    assert none_left == 0


def test_runner_stop_lets_a_short_iteration_finish(enabled):
    """The other half of the contract: a budget that comfortably covers the
    in-flight iteration must let it commit rather than cancelling it."""
    from app.services._backfill import BackfillRunner

    finished: list[str] = []

    async def _quick():
        await asyncio.sleep(0.05)
        finished.append("done")
        return 0

    async def _drive():
        runner = BackfillRunner("t_shield_ok", _quick, idle_secs=1)
        runner.start()
        await asyncio.sleep(0.01)
        await runner.stop(timeout=1.0)
        return runner

    runner = asyncio.run(_drive())
    assert finished == ["done"], "a fitting iteration must be allowed to finish"
    assert not runner._inflight


def test_maintenance_is_stopped_even_when_the_drain_overruns(enabled, monkeypatch):
    """Wrapping the whole shutdown in a single `wait_for` meant an expiry
    during the drain skipped `_maintainer.stop()` outright — its stop event
    never set, its task never awaited — so a shielded rollup kept running
    against a pool `close_pool()` was about to tear down.

    Every phase must be reached, each under its own bound."""
    monkeypatch.setattr(settings.tool_usage, "shutdown_deadline_secs", 0.5)
    stopped: list[str] = []

    async def _hang():
        await asyncio.sleep(30)                      # drain never completes

    async def _flusher_stop(timeout=120.0):
        stopped.append("flusher")

    async def _maintainer_stop(timeout=120.0):
        stopped.append("maintainer")

    monkeypatch.setattr(tool_usage, "get_pool", _hang)
    monkeypatch.setattr(tool_usage._flusher, "stop", _flusher_stop)
    monkeypatch.setattr(tool_usage._maintainer, "stop", _maintainer_stop)
    tool_usage.record("akb_put", {"vault": "v"}, _User(), {"ok": True})

    asyncio.run(tool_usage.stop())

    assert stopped == ["flusher", "maintainer"], (
        "an overrunning drain must not skip stopping the maintenance runner"
    )
    assert tool_usage.queue_depth() == 1, (
        "the cancelled drain's batch is back in the queue and therefore counted"
    )


def test_stop_never_propagates(monkeypatch):
    """`lifecycle.stop_workers()` awaits each worker's `stop()` in sequence, so
    an exception escaping one skips every worker after it.

    Observed live: a config field this function reads on its first line was
    missing, `AttributeError` propagated out of `stop_workers()`, and the
    shutdown of events_publisher, metadata_worker, embed_worker and the rest
    never ran. The module's contract already says shutdown must not raise —
    the budget read simply sat outside the guard."""
    class _Exploding:
        @property
        def tool_usage(self):
            raise AttributeError("config drift")

    monkeypatch.setattr(tool_usage, "settings", _Exploding())
    asyncio.run(tool_usage.stop())          # must not raise


def test_flush_is_awaitable_for_the_backfill_runner():
    """`BackfillRunner` awaits its callback — a non-coroutine crashes the loop."""
    assert inspect.iscoroutinefunction(tool_usage.flush_once)
    assert inspect.iscoroutinefunction(tool_usage.rollup_once)


# ── Rollup + purge ──────────────────────────────────────────────


def test_rollup_claims_rows_rather_than_tracking_a_sequence_watermark():
    """A `MAX(id)` high-water mark is NOT a commit-order watermark: Postgres
    allocates sequence values before commit, so the mark can advance past a
    lower id whose transaction is still open. That row is then never folded and
    the purge predicate deletes it anyway — a silent permanent undercount that
    only appears once a second inserter exists (scale-out, `--workers 2`).

    Per-row claim state has no ordering assumption at all. This test pins the
    *shape*; the behaviour itself is proven in `test_tool_usage_e2e.py`, which
    runs concurrent writers against a real database."""
    src = inspect.getsource(tool_usage)
    assert "last_rolled_id" not in src, (
        "sequence watermarks are unsafe here — claim rows instead"
    )
    rollup_sql = tool_usage._ROLLUP_SQL
    assert "SET rolled_at" in rollup_sql, "the fold must stamp the rows it counts"
    assert "SKIP LOCKED" in rollup_sql, "a second runner must take a different slice"

    purge_sql = tool_usage._PURGE_SQL
    assert "rolled_at IS NOT NULL" in purge_sql, (
        "an unfolded row must survive purge however old it is"
    )


def test_purge_runs_even_while_the_rollup_still_has_work(monkeypatch):
    """An earlier version returned early whenever the rollup folded anything,
    waiting for a globally quiet moment before purging. Sustained traffic never
    produces one, so purge could be starved indefinitely while already-folded
    rows piled up — unbounded growth of the table the retention policy exists
    to bound.

    The wait was never needed: `rolled_at IS NOT NULL` makes each row
    individually safe to delete regardless of what else is unfolded."""
    calls: list[str] = []

    async def _rollup():
        calls.append("rollup")
        return 5                       # a backlog remains, tick after tick

    async def _purge():
        calls.append("purge")
        return 3

    monkeypatch.setattr(tool_usage, "rollup_once", _rollup)
    monkeypatch.setattr(tool_usage, "purge_once", _purge)

    assert asyncio.run(tool_usage.maintenance_once()) == 8
    assert calls == ["rollup", "purge"], (
        "purge must not be gated on the rollup reaching zero"
    )


@pytest.mark.parametrize("failing", ["rollup", "purge"])
def test_one_failing_leg_does_not_suppress_the_other(monkeypatch, failing):
    """The two legs must fail independently.

    Sequenced in one coroutine with no isolation, a purge error after a
    successful 5,000-row fold discards the rollup's count too: `BackfillRunner`
    turns the exception into `done = 0` and then sleeps the *entire* idle
    interval — an hour — so throughput collapses to one batch per hour for as
    long as the other leg keeps failing. In the mirror case a rollup error
    stops purge being attempted at all, re-creating the starvation this
    function was just fixed to avoid."""
    done: list[str] = []

    async def _ok(name):
        done.append(name)
        return 5

    async def _fail(name):
        raise RuntimeError(f"{name} exploded")

    monkeypatch.setattr(
        tool_usage, "rollup_once",
        (lambda: _fail("rollup")) if failing == "rollup" else (lambda: _ok("rollup")),
    )
    monkeypatch.setattr(
        tool_usage, "purge_once",
        (lambda: _fail("purge")) if failing == "purge" else (lambda: _ok("purge")),
    )

    moved = asyncio.run(tool_usage.maintenance_once())

    survivor = "purge" if failing == "rollup" else "rollup"
    assert done == [survivor], f"{survivor} must still run when {failing} fails"
    assert moved == 5, "the successful leg's work must still be reported"


def test_purge_cutoff_is_a_whole_day_boundary():
    """Retention advances a whole UTC day at a time.

    This began as a correctness fix — a recompute-style rollup would re-derive
    a half-purged day from the surviving fragment and overwrite its total. The
    rollup is now additive and per-row, so that failure is gone; the boundary
    stays because it keeps "this day is gone" from being half-true for anyone
    joining raw rows against the aggregate."""
    from datetime import datetime, timezone

    cutoff = tool_usage._purge_cutoff(
        datetime(2026, 7, 28, 13, 47, 31, 12345, tzinfo=timezone.utc)
    )
    assert (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) == (0, 0, 0, 0)
    assert cutoff.tzinfo is timezone.utc


def test_maintenance_work_is_bounded_per_statement():
    """Catching up after an outage must not become one transaction big enough
    to hit the statement timeout and spike WAL/bloat. Both statements take a
    LIMIT, and a non-zero rollup keeps the runner looping so a backlog still
    drains promptly."""
    src = inspect.getsource(tool_usage)
    for const in ("_ROLLUP_SQL", "_PURGE_SQL"):
        sql = src.split(f"{const} = ")[1].split('"""')[1]
        assert "LIMIT $" in sql, f"{const} must bound how much one statement moves"


# ── Structural guard: the chokepoint must stay wired ────────────


def _call_tool_node() -> ast.AsyncFunctionDef:
    path = pathlib.Path(__file__).resolve().parents[1] / "mcp_server" / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "call_tool":
            return node
    raise AssertionError("mcp_server.server.call_tool not found")


def _records_usage(body: list[ast.stmt]) -> bool:
    """True if this branch calls `tool_usage.record(...)` — matched on the AST
    so a comment, a string, or the similarly-named `audit_log.record_tool`
    cannot satisfy it."""
    for stmt in body:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "tool_usage"
            ):
                return True
    return False


def test_dispatch_records_usage_on_both_success_and_error():
    """Every MCP tool call passes through `call_tool`, and it has two exits —
    the success return and the `except Exception` envelope. `audit_log` is
    wired into both; usage must be too, or crashing tools (the ones most worth
    counting) become invisible."""
    fn = _call_tool_node()
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, "call_tool is expected to wrap dispatch in try/except"

    assert any(_records_usage(t.body) for t in tries), (
        "tool_usage.record is missing from the SUCCESS path of call_tool"
    )
    assert any(
        _records_usage(h.body) for t in tries for h in t.handlers
    ), "tool_usage.record is missing from the ERROR path of call_tool"


def test_error_path_records_only_when_the_success_path_did_not():
    """`call_tool` records after dispatch and then serialises inside the SAME
    `try`, so a `json.dumps` failure falls through to the `except`. Without a
    guard that branch logs the very same invocation a second time — one call,
    two rows, an inflated count and a self-contradicting audit trail.

    Serialising first would also fix the duplicate, but at a worse price: a
    handler that already committed its write would be recorded purely as an
    error. Recording the dispatch outcome and suppressing the second write
    keeps the business outcome honest."""
    fn = _call_tool_node()
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    recording = [h for h in handlers if _records_usage(h.body)]
    assert recording, "the error path must still record genuinely failed calls"

    for h in recording:
        guarded = any(
            isinstance(node, ast.If) and _records_usage(node.body)
            for node in ast.walk(ast.Module(body=h.body, type_ignores=[]))
        )
        assert guarded, (
            "the error path must skip recording when the success path already "
            "recorded this invocation"
        )


def test_serialisation_failure_records_the_call_exactly_once(monkeypatch, tmp_path):
    """The runtime counterpart to the AST guard above. `json.dumps` runs inside
    the same `try` as the recording calls, so a result the encoder cannot
    handle lands in the `except` — which without the guard logs the same
    invocation a second time as an error.

    A self-referencing dict defeats `default=str` (it recurses), which is the
    real shape of this failure."""
    # The module builds a GitService at import, which mkdirs the production
    # `git_storage_path` — redirect it first (same pattern as
    # test_activity_git_offload_unit.py).
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from mcp_server import server as srv

    circular: dict = {"ok": True}
    circular["self"] = circular

    async def _dispatch(name, args, user):
        return circular

    usage: list = []
    audit: list = []
    monkeypatch.setattr(srv, "_dispatch", _dispatch)
    monkeypatch.setattr(srv.tool_usage, "record", lambda *a, **k: usage.append(a[0]))
    monkeypatch.setattr(srv.audit_log, "record_tool", lambda *a, **k: audit.append(a[0]))

    out = asyncio.run(srv.call_tool("akb_put", {"vault": "v"}))

    assert len(usage) == 1, f"usage recorded {len(usage)} times for one call"
    assert len(audit) == 1, f"audit recorded {len(audit)} times for one call"
    # The caller still gets a well-formed error envelope.
    assert "error" in out[0].text


def test_lifecycle_starts_and_stops_the_workers():
    """The chokepoint guards above cover half the wiring; this is the other
    half, and it is the half the design doc names as fragile.

    `tool_usage.start()` must NOT sit behind `settings.tool_usage.enabled` —
    the maintenance runner has to keep folding and pruning what was already
    collected after collection is turned off, which is precisely the coupling
    that lets the `events` outbox grow forever. Folding it under the flag
    looks like a tidy-up and would silently stop retention while every unit
    test still passes, because they call the legs directly."""
    path = pathlib.Path(__file__).resolve().parents[1] / "app/services/lifecycle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _calls(fn_name: str) -> list[ast.Call]:
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == fn_name:
                return [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            if isinstance(node, ast.FunctionDef) and node.name == fn_name:
                return [n for n in ast.walk(node) if isinstance(n, ast.Call)]
        raise AssertionError(f"lifecycle.{fn_name} not found")

    def _has(calls, attr: str) -> bool:
        return any(
            isinstance(c.func, ast.Attribute) and c.func.attr == attr
            and isinstance(c.func.value, ast.Name) and c.func.value.id == "tool_usage"
            for c in calls
        )

    assert _has(_calls("_start_api_local"), "start"), (
        "the API-local lifecycle must start tool-usage maintenance"
    )
    assert any(
        isinstance(call.func, ast.Name) and call.func.id == "_start_api_local"
        for call in _calls("start_workers")
    ), "all-in-one lifecycle must compose the API-local sinks"
    assert _has(_calls("stop_workers"), "stop"), "lifecycle must stop the workers"

    # `start()` must be unconditional — find it and check no enclosing `if`.
    src = path.read_text(encoding="utf-8")
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            body_calls = [n for stmt in node.body for n in ast.walk(stmt)
                          if isinstance(n, ast.Call)]
            assert not _has(body_calls, "start"), (
                "tool_usage.start() must not be gated on a flag — rollup/purge "
                "have to keep running when collection is disabled"
            )
    assert "tool_usage" in src


def test_usage_sink_is_independent_of_the_audit_flags():
    """Usage must not ride on `audit.log_reads`. `akb_grep(replace=)` was
    dropped from the audit trail by exactly that coupling (PR #313) — reusing
    the flag here would silently delete read-tool usage, which is most of it.

    Checked on the AST, not the text, so the module can still *explain* the
    coupling it is avoiding without tripping its own guard."""
    tree = ast.parse(inspect.getsource(tool_usage))
    reads = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "log_reads" not in reads, "usage must not be gated by an audit flag"

    audit_chains = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "audit"
        and isinstance(node.value, ast.Name)
        and node.value.id == "settings"
    ]
    assert audit_chains == [], "usage settings must be their own section"
