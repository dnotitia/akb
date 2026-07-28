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
    ({"vault": "explicit", "uri": "akb://other/doc/x.md"}, "explicit"),
    ({"query": "no vault anywhere"}, None),
    ({"uri": "not-a-uri"}, None),
])
def test_vault_is_derived_from_uris_too(enabled, args, expected):
    """20 of the 43 tools take no `vault` argument at all — `akb_get`,
    `akb_update`, `akb_edit`, `akb_delete`, `akb_move`, `akb_link` and friends
    address resources by `akb://` URI, because the canonical API deliberately
    dropped the redundant vault parameter. Reading only `args.vault` would
    leave the per-vault dimension NULL on most rows and quietly make the
    "which vault uses what" question unanswerable."""
    tool_usage.record("akb_x", args, _User(), {"ok": True})
    assert tool_usage.drain(1)[0].vault == expected


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


# ── Flush ───────────────────────────────────────────────────────


class _Conn:
    def __init__(self, box):
        self.box = box

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
