"""Contract tests for the `/stats` listener, its sampler, and the schema.

The properties under test are the ones a consumer cannot check for itself:

* an unmeasurable number is ABSENT, never 0,
* the previous day's call volume is decided once and never re-decided,
* a request never triggers a computation, and a failed sample never blanks
  the surface,
* the payload validates against the JSON Schema this repo publishes for it.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import signal
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import jsonschema
import pytest

from app.config import settings
from app.logging_redaction import _FILTER as REDACTION_FILTER
from app.logging_redaction import install_secret_redaction
from app.stats import listener, sampler

# `asyncio_mode = "auto"` (pyproject) runs the coroutine tests; the sync ones
# here are ordinary functions and must not carry an asyncio mark.
_PKG = Path(sampler.__file__).parent
SCHEMA = json.loads((_PKG / "schema_v1.json").read_text())
GOLDEN = json.loads((_PKG / "golden_v1.json").read_text())
GOLDEN_UNMEASURED = json.loads((_PKG / "golden_v1_unmeasured.json").read_text())

_UTC = timezone.utc
# A mid-day clock: the most recent closed-and-settled UTC day is 2026-08-20.
_NOW = datetime(2026, 8, 21, 9, 0, tzinfo=_UTC)


# ── a fake database that behaves like the real one for these statements ──


class FakeDatabase:
    """Just enough Postgres to exercise the sampler's own logic.

    `tenant_activity_daily` is a real dict keyed on day, so "fold once" and
    "survives a restart" are observable properties rather than assertions about
    a mock's call list.
    """

    def __init__(self):
        self.db_bytes = 8657174528
        self.relation_sizes = {("vector_index", "chunks"): 3_000_000_000,
                               ("vector_index", "posting"): 221_225_472}
        self.corpus = {
            "vault_count": 7,
            "collection_count": 63,
            "doc_count": 2841,
            "vector_chunk_count": 19204,
        }
        self.files = {"file_count": 128, "unsized": 0, "file_bytes": 41637294}
        self.tool_calls = {"calls_read": 4192, "calls_write": 317, "active_actors": 11}
        self.activity_rows: dict[date, dict] = {}
        self.transactions: list[dict] = []
        self.fail_with: Exception | None = None


class FakeConn:
    def __init__(self, db: FakeDatabase):
        self._db = db

    async def fetchval(self, query, *args):
        if self._db.fail_with is not None:
            raise self._db.fail_with
        if "pg_database_size" in query:
            return self._db.db_bytes
        if "pg_total_relation_size" in query:
            return self._db.relation_sizes.get((args[0], args[1]))
        if "SELECT 1 FROM tenant_activity_daily" in query:
            return 1 if args[0] in self._db.activity_rows else None
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query, *args):
        if self._db.fail_with is not None:
            raise self._db.fail_with
        if "AS vault_count" in query:
            return dict(self._db.corpus)
        if "FROM vault_files" in query:
            return dict(self._db.files)
        if "FROM tool_calls" in query:
            return dict(self._db.tool_calls)
        if "FROM tenant_activity_daily" in query and "ORDER BY day DESC" in query:
            if not self._db.activity_rows:
                return None
            newest = max(self._db.activity_rows)
            return dict(self._db.activity_rows[newest])
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query, *args):
        if "INSERT INTO tenant_activity_daily" in query:
            day = args[0]
            # ON CONFLICT (day) DO NOTHING
            self._db.activity_rows.setdefault(
                day,
                {
                    "day": day,
                    "window_start": args[1],
                    "window_end": args[2],
                    "calls_read": args[3],
                    "calls_write": args[4],
                    "active_actors": args[5],
                },
            )
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {query}")

    def transaction(self, **kwargs):
        self._db.transactions.append(kwargs)

        @asynccontextmanager
        async def _tx():
            yield

        return _tx()


def fake_pool(db: FakeDatabase):
    class _Pool:
        def acquire(self):
            @asynccontextmanager
            async def _acquire():
                yield FakeConn(db)

            return _acquire()

    async def _get_pool():
        return _Pool()

    return _get_pool


@pytest.fixture
def db(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(sampler, "get_pool", fake_pool(database))
    monkeypatch.setattr(settings.tool_usage, "enabled", True)
    monkeypatch.setattr(settings, "vector_store_driver", "pgvector")
    monkeypatch.setattr(settings, "vector_store_dsn", "")
    monkeypatch.setattr(settings, "vector_store_schema", "vector_index")
    sampler.reset()
    yield database
    sampler.reset()


# ── schema + golden fixtures ─────────────────────────────────────────────


def test_schema_is_a_valid_2020_12_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("fixture", [GOLDEN, GOLDEN_UNMEASURED], ids=["full", "unmeasured"])
def test_golden_fixtures_validate(fixture):
    jsonschema.validate(fixture, SCHEMA)


def _assert_every_numeric_field_is_nullable_and_optional(schema):
    for section in ("storage", "corpus", "activity"):
        block = schema["properties"][section]
        required = set(block.get("required", ()))
        for name, spec in block["properties"].items():
            types = spec["type"]
            # JSON Schema allows both `"integer"` and `["integer", "null"]`;
            # the string form is exactly what a hand-added measurement would
            # look like, so it must be examined, not skipped.
            types = [types] if isinstance(types, str) else list(types)
            if "integer" not in types and "number" not in types:
                continue  # timestamps, not measurements
            assert "null" in types, f"{section}.{name} must accept null"
            assert name not in required, f"{section}.{name} must not be required"


def test_schema_marks_every_numeric_field_nullable_and_optional():
    """The encoding contract, asserted against the published artifact.

    A consumer that vendors this file has to be told, by the file itself, that
    every number can be missing. A required numeric field would let a
    generator emit a non-pointer integer and silently turn absence into 0.
    """
    _assert_every_numeric_field_is_nullable_and_optional(SCHEMA)


def test_the_numeric_field_check_catches_a_string_typed_measurement():
    """The check must fail for the field it is most likely to be handed.

    `"type": "integer"` (string form) is the shape a new measurement would be
    written in; a check that only examined the list form would wave it
    through as a timestamp.
    """
    schema = copy.deepcopy(SCHEMA)
    corpus = schema["properties"]["corpus"]
    corpus["properties"]["table_count"] = {"type": "integer"}
    corpus["required"] = [*corpus.get("required", ()), "table_count"]
    with pytest.raises(AssertionError, match="corpus.table_count"):
        _assert_every_numeric_field_is_nullable_and_optional(schema)


async def test_computed_payload_has_exactly_the_golden_shape(db):
    """A fully measurable environment produces the documented payload.

    Compared key-by-key against the fixture rather than field-by-field in the
    test, so the fixture stays the thing that has to be updated when the shape
    changes — which is also what the consumer vendors.
    """
    payload = await sampler.compute(now=_NOW)

    jsonschema.validate(payload, SCHEMA)
    assert _shape(payload) == _shape(GOLDEN)
    assert payload["storage"]["vector_bytes"] == 3_221_225_472
    assert payload["activity"]["calls_read"] == 4192


def _shape(obj):
    if isinstance(obj, dict):
        return {key: _shape(value) for key, value in sorted(obj.items())}
    return type(obj).__name__


# ── absent is not zero ───────────────────────────────────────────────────


async def test_unmeasurable_fields_are_absent_rather_than_zero(db):
    """Every field this test makes unmeasurable must vanish, not become 0."""
    db.relation_sizes = {}  # vector index not materialised yet
    db.files = {"file_count": 128, "unsized": 3, "file_bytes": 41637294}
    closed_day = date(2026, 8, 20)  # what `_NOW` targets
    db.activity_rows[closed_day] = {
        "day": closed_day,
        "window_start": datetime(2026, 8, 20, tzinfo=_UTC),
        "window_end": datetime(2026, 8, 21, tzinfo=_UTC),
        "calls_read": None,
        "calls_write": None,
        "active_actors": None,
    }

    payload = await sampler.compute(now=_NOW)

    jsonschema.validate(payload, SCHEMA)
    assert "vector_bytes" not in payload["storage"]
    assert "file_bytes" not in payload["storage"]
    assert "distilled_doc_count" not in payload["corpus"]
    for absent in ("calls_read", "calls_write", "active_actors"):
        assert absent not in payload["activity"]
    # The measurable neighbours are still there — absence is per field.
    assert payload["storage"]["file_count"] == 128
    assert payload["storage"]["db_bytes"] == 8657174528
    assert _shape(payload) == _shape(GOLDEN_UNMEASURED)


@pytest.mark.parametrize(
    "driver,dsn",
    [("qdrant", ""), ("seahorse-cloud", ""), ("pgvector", "postgresql://elsewhere/vec")],
    ids=["qdrant", "seahorse", "pgvector-own-dsn"],
)
async def test_vector_bytes_absent_when_the_index_is_not_in_this_database(db, monkeypatch, driver, dsn):
    """`vector_bytes` is documented as a subset of `db_bytes`.

    Reporting bytes that live in another store (or another database) would
    break that containment and invite a consumer to sum the two.
    """
    monkeypatch.setattr(settings, "vector_store_driver", driver)
    monkeypatch.setattr(settings, "vector_store_dsn", dsn)
    payload = await sampler.compute()
    assert "vector_bytes" not in payload["storage"]


async def test_vector_bytes_absent_unless_every_index_relation_is_measured(db):
    """Half an index is not a smaller index.

    `pg_total_relation_size` answers per relation, so one missing relation
    would otherwise leave a sum over the rest — a partial total that reads
    exactly like the whole one. Both relations are created in one batch, so
    this is theoretical today; the field keeps the principle literal.
    """
    db.relation_sizes = {("vector_index", "chunks"): 3_000_000_000}  # no `posting`
    payload = await sampler.compute(now=_NOW)
    assert "vector_bytes" not in payload["storage"]


async def test_vector_bytes_counts_only_the_relations_the_sparse_shape_creates(db, monkeypatch):
    """The `arrays` shape keeps sparse terms inside `chunks`; there is no
    `posting` table to be missing, so its absence must not blank the field."""
    monkeypatch.setattr(settings, "vector_store_sparse_shape", "arrays")
    db.relation_sizes = {("vector_index", "chunks"): 3_000_000_000}
    payload = await sampler.compute(now=_NOW)
    assert payload["storage"]["vector_bytes"] == 3_000_000_000


async def test_file_bytes_absent_when_any_confirmed_file_has_no_size(db):
    db.files = {"file_count": 5, "unsized": 1, "file_bytes": 900}
    payload = await sampler.compute()
    assert "file_bytes" not in payload["storage"]
    assert payload["storage"]["file_count"] == 5


# ── corpus consistency ───────────────────────────────────────────────────


async def test_corpus_is_read_in_a_repeatable_read_snapshot(db):
    await sampler.compute()
    assert {"isolation": "repeatable_read", "readonly": True} in db.transactions


# ── activity: closed once, forever ───────────────────────────────────────


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 8, 21, 9, 0, tzinfo=_UTC), date(2026, 8, 20)),
        (datetime(2026, 8, 21, 0, 5, tzinfo=_UTC), date(2026, 8, 20)),
        # Inside the grace window the freshly closed day is still settling.
        (datetime(2026, 8, 21, 0, 1, tzinfo=_UTC), date(2026, 8, 19)),
        (datetime(2026, 8, 21, 23, 59, tzinfo=_UTC), date(2026, 8, 20)),
    ],
    ids=["mid-day", "at-grace", "inside-grace", "end-of-day"],
)
def test_target_day_defers_a_day_that_is_still_settling(now, expected):
    assert sampler.target_activity_day(now, timedelta(minutes=5)) == expected


async def test_activity_window_is_finalized_once_and_survives_a_restart(db):
    """The value a consumer first sees must be the value it always sees.

    The second pass here is a restarted process: the module cache is cleared
    and `tool_calls` now answers differently (a purge, a late flush, tracking
    toggled). The served window must not move.
    """
    first = await sampler.compute(now=_NOW)
    assert first["activity"]["calls_read"] == 4192
    assert first["activity"]["window_start"] == "2026-08-20T00:00:00Z"
    assert first["activity"]["window_end"] == "2026-08-21T00:00:00Z"

    db.tool_calls = {"calls_read": 1, "calls_write": 1, "active_actors": 1}
    sampler.reset()  # process restart

    second = await sampler.compute(now=_NOW)
    assert second["activity"] == first["activity"]
    assert len(db.activity_rows) == 1


async def test_activity_counts_are_unknown_when_usage_tracking_is_off(db, monkeypatch):
    """A window closed with tracking off is unknown, not quiet.

    The fold is permanent, so writing 0 here would publish "nobody used this
    tenant yesterday" as an uncorrectable fact.
    """
    monkeypatch.setattr(settings.tool_usage, "enabled", False)

    payload = await sampler.compute(now=_NOW)

    assert payload["activity"]["window_start"] == "2026-08-20T00:00:00Z"
    for absent in ("calls_read", "calls_write", "active_actors"):
        assert absent not in payload["activity"]


async def test_activity_counts_are_unknown_when_the_purge_may_have_reached_the_window(db, monkeypatch):
    """A window behind the retention boundary is unknown, not whatever is left.

    `tool_usage` purges rolled-up rows older than `raw_retention_days` (whole
    UTC days) on its own cadence. With the minimum retention of 1, a sample
    inside the grace window targets the day before yesterday — a window the
    purge may already have emptied — and a count over the remainder would be
    frozen as that day's volume forever.
    """
    monkeypatch.setattr(settings.tool_usage, "raw_retention_days", 1)

    inside_grace = datetime(2026, 8, 21, 0, 1, tzinfo=_UTC)  # targets 2026-08-19
    payload = await sampler.compute(now=inside_grace)

    assert payload["activity"]["window_start"] == "2026-08-19T00:00:00Z"
    for absent in ("calls_read", "calls_write", "active_actors"):
        assert absent not in payload["activity"]

    # Same retention, a settled clock: yesterday is still in front of the
    # boundary and is measured. The rule is about the purge, not the setting.
    sampler.reset()
    db.activity_rows.clear()
    measured = await sampler.compute(now=_NOW)
    assert measured["activity"]["window_start"] == "2026-08-20T00:00:00Z"
    assert measured["activity"]["calls_read"] == 4192


async def test_activity_section_is_absent_before_any_day_is_folded(db, monkeypatch):
    """`_fold` inserts the target day, so force the "nothing folded" state."""

    async def _no_fold(conn, now):
        return None

    monkeypatch.setattr(sampler, "_fold_activity_day", _no_fold)
    payload = await sampler.compute()
    assert "activity" not in payload
    jsonschema.validate(payload, SCHEMA)


# ── cache lifecycle ──────────────────────────────────────────────────────


async def _get_stats():
    response = await listener.stats()
    return response.status_code, json.loads(bytes(response.body))


async def test_stats_is_503_until_the_first_sample_completes(db):
    status, body = await _get_stats()
    assert status == 503
    assert body["status"] == "unavailable"


async def test_stats_serves_the_sampled_snapshot(db):
    assert await sampler.sample_once() == 0
    status, body = await _get_stats()
    assert status == 200
    jsonschema.validate(body, SCHEMA)
    assert body["schema_version"] == sampler.SCHEMA_VERSION


async def test_a_failed_sample_keeps_serving_the_last_snapshot(db):
    await sampler.sample_once()
    _, good = await _get_stats()

    db.fail_with = RuntimeError("database went away")
    assert await sampler.sample_once() == 0  # the loop must not die

    status, body = await _get_stats()
    assert status == 200
    assert body == good
    assert sampler.stats()["failures"] == 1
    assert "database went away" in sampler.stats()["last_error"]


async def test_serving_never_recomputes(db, monkeypatch):
    """A poll must not reach the database, however often it arrives."""
    await sampler.sample_once()

    async def _forbidden():
        raise AssertionError("/stats must be served from cache")

    monkeypatch.setattr(sampler, "get_pool", _forbidden)
    for _ in range(5):
        status, _body = await _get_stats()
        assert status == 200


# ── listener composition ─────────────────────────────────────────────────


def test_listener_is_not_composed_without_a_port(monkeypatch):
    monkeypatch.delenv(listener.PORT_ENV_VAR, raising=False)
    monkeypatch.setattr(settings.stats, "port", None)
    assert listener.configured_port() is None
    assert listener.start() is False
    assert listener.is_running() is False


def test_environment_port_overrides_the_config_file(monkeypatch):
    monkeypatch.setattr(settings.stats, "port", 9100)
    monkeypatch.setenv(listener.PORT_ENV_VAR, "9999")
    assert listener.configured_port() == 9999


@pytest.mark.parametrize("value", ["909O", "0", "65536", "-1"])
def test_a_malformed_environment_port_is_fatal(monkeypatch, value):
    """Not a silent fall back to "off".

    A stats port that quietly fails to bind produces a deployment that looks
    healthy and is invisible to the control plane that provisioned it.
    """
    monkeypatch.setenv(listener.PORT_ENV_VAR, value)
    with pytest.raises(RuntimeError):
        listener.configured_port()


async def test_the_stats_server_leaves_process_signal_handlers_alone():
    """Uvicorn installs SIGTERM/SIGINT handlers for the duration of `serve()`.

    A second server doing that inside the first one's lifespan would steal the
    API server's shutdown signal — the pod would be SIGKILLed at the end of its
    grace period with the API never told to drain.
    """
    # The override is only worth anything while `Server` still calls a hook by
    # this name; a rename would turn it into dead code and `serve()` would
    # install handlers again with this test none the wiser. The `==` pin on
    # uvicorn is what protects production; this makes a bump fail here first.
    assert "capture_signals" in vars(listener.uvicorn.Server)

    # `log_config=None`: a default `Config` reconfigures the process-wide
    # uvicorn loggers, which is exactly what the listener no longer does.
    server = listener._NoSignalServer(listener.uvicorn.Config(app=listener.stats_app, log_config=None))
    before = signal.getsignal(signal.SIGTERM)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGTERM) is before
    assert signal.getsignal(signal.SIGTERM) is before


async def test_stop_is_safe_when_the_listener_was_never_started():
    await asyncio.wait_for(listener.stop(), timeout=2.0)


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def uvicorn_logging_restored():
    """Put the three process-wide uvicorn loggers back the way pytest had them."""
    saved = {}
    for name in _UVICORN_LOGGERS:
        log = logging.getLogger(name)
        saved[name] = (list(log.handlers), log.level, log.propagate, list(log.filters))
    yield
    for name, (handlers, level, propagate, filters) in saved.items():
        log = logging.getLogger(name)
        log.handlers = handlers
        log.setLevel(level)
        log.propagate = propagate
        log.filters = filters


def _uvicorn_logging_state():
    return {
        name: (
            tuple(logging.getLogger(name).handlers),
            tuple(tuple(handler.filters) for handler in logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in _UVICORN_LOGGERS
    }


async def test_starting_the_listener_leaves_the_process_wide_uvicorn_logging_alone(
    monkeypatch, uvicorn_logging_restored
):
    """`uvicorn.Config` configures logging for the whole process, not a server.

    The API server's `uvicorn`, `uvicorn.error` and `uvicorn.access` loggers
    are the same objects this listener's server would log through. A second
    `Config` built with the default `log_config` re-runs `dictConfig` over
    them — replacing the handler the secret-redaction filter is installed on —
    and `access_log=False` / `log_level=` would strip and re-level them. The
    first cut of this listener did exactly that: setting `stats.port` silenced
    the API's access log and let `uvicorn.error` records bypass redaction.

    Reproduces the state the API server is in when the lifespan starts this
    listener (uvicorn's own logging config, then the redaction install) and
    asserts that `start()` changes none of it: same handler objects, same
    filters on them, same levels, same propagation.
    """
    # What `uvicorn app.main:app` (the Dockerfile) does for the API server,
    # then what the lifespan does on top of it.
    listener.uvicorn.Config(app=listener.stats_app, log_level="info")
    install_secret_redaction()
    before = _uvicorn_logging_state()
    # Sanity: there is something to clobber, and the redaction is on it.
    assert logging.getLogger("uvicorn.access").handlers
    assert logging.getLogger("uvicorn.error").level == logging.INFO
    assert all(REDACTION_FILTER in handler.filters for handler in logging.getLogger("uvicorn").handlers)

    monkeypatch.delenv(listener.PORT_ENV_VAR, raising=False)
    monkeypatch.setattr(settings.stats, "host", "127.0.0.1")
    monkeypatch.setattr(settings.stats, "port", _free_port())

    assert listener.start() is True
    try:
        assert _uvicorn_logging_state() == before
    finally:
        await listener.stop(timeout=5.0)


# ── /health: queue-head age ──────────────────────────────────────────────
#
# The other half of the monitoring contract. The indexing backlog itself was
# already reported; what a poller could not tell was whether the backlog is
# moving or stuck, which needs the age of the oldest item still waiting.


class _QueueConn:
    """A connection that answers only the indexing-queue aggregate."""

    def __init__(self, oldest):
        self.oldest = oldest

    async def fetchrow(self, query, *args):
        assert "FROM chunks" in query
        return {
            "pending": 4,
            "retrying": 0,
            "abandoned": 0,
            "indexed": 100,
            "oldest_pending": self.oldest,
        }


def _queue_pool(oldest):
    class _Pool:
        def acquire(self):
            @asynccontextmanager
            async def _acquire():
                yield _QueueConn(oldest)

            return _acquire()

    async def _get_pool():
        return _Pool()

    return _get_pool


async def test_pending_stats_reports_the_age_of_the_queue_head(monkeypatch):
    from app.services import delete_worker, embed_worker

    enqueued = datetime(2026, 8, 21, 7, 30, tzinfo=_UTC)
    monkeypatch.setattr(embed_worker, "get_pool", _queue_pool(enqueued))
    monkeypatch.setattr(delete_worker, "delete_outbox_stats", _async_return({}))

    stats = await embed_worker.pending_stats()
    assert stats["upsert"]["oldest_pending_enqueued_at"] == enqueued.isoformat()


async def test_pending_stats_omits_the_age_when_the_queue_is_empty(monkeypatch):
    """Absent, not epoch: a zero timestamp renders as an infinitely stale queue."""
    from app.services import delete_worker, embed_worker

    monkeypatch.setattr(embed_worker, "get_pool", _queue_pool(None))
    monkeypatch.setattr(delete_worker, "delete_outbox_stats", _async_return({}))

    stats = await embed_worker.pending_stats()
    assert "oldest_pending_enqueued_at" not in stats["upsert"]


def _async_return(value):
    async def _call(*args, **kwargs):
        return value

    return _call


@pytest.fixture
def health_app(monkeypatch, tmp_path):
    """Stub every collaborator `/health` fans out to, except the queue stats."""
    # Importing the API app constructs the legacy revision backend, which
    # creates its Git storage root eagerly. Redirect it before the import.
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    import app.main as main
    from app.services import (
        asset_gc_worker,
        events_publisher,
        external_git_poller,
        metadata_worker,
        native_file_projection,
        queue_rescuer,
        sparse_encoder,
        vault_backfill,
    )

    class _Store:
        async def health(self):
            return True

    monkeypatch.setattr(main, "get_vector_store", lambda: _Store())
    monkeypatch.setattr(main, "worker_lifecycle_snapshot", dict)
    monkeypatch.setattr(queue_rescuer, "snapshot", dict)
    monkeypatch.setattr(vault_backfill, "pending_stats", _async_return({}))
    monkeypatch.setattr(sparse_encoder, "stats_snapshot", _async_return({}))
    monkeypatch.setattr(native_file_projection, "pending_stats", _async_return({}))
    for module in (external_git_poller, asset_gc_worker, metadata_worker, events_publisher):
        monkeypatch.setattr(module, "pending_stats", _async_return({}))
    return main


async def test_health_promotes_the_queue_head_age_to_the_top_level(health_app, monkeypatch):
    """The contract term lives at the top level, not inside an operational blob.

    A consumer must not have to know that the value happens to be produced by
    the vector-store backfill reporter, which is free to be reorganised.
    """
    from app.services import delete_worker, embed_worker

    enqueued = datetime(2026, 8, 21, 7, 30, tzinfo=_UTC)
    monkeypatch.setattr(embed_worker, "get_pool", _queue_pool(enqueued))
    monkeypatch.setattr(delete_worker, "delete_outbox_stats", _async_return({}))

    result = await health_app.health(user=None)

    assert result["oldest_pending_enqueued_at"] == enqueued.isoformat()
    # Unauthenticated callers get it: it is backlog data, like its neighbours.
    assert "audit" not in result


async def test_health_omits_the_queue_head_age_when_nothing_is_pending(health_app, monkeypatch):
    from app.services import delete_worker, embed_worker

    monkeypatch.setattr(embed_worker, "get_pool", _queue_pool(None))
    monkeypatch.setattr(delete_worker, "delete_outbox_stats", _async_return({}))

    result = await health_app.health(user=None)

    assert "oldest_pending_enqueued_at" not in result


async def test_health_surfaces_native_file_projection_exhaustion_as_degraded(health_app, monkeypatch):
    """A terminal S3-to-Native intent must not look like a healthy projection."""
    from app.services import native_file_projection

    diagnostic = {
        "pending": 0,
        "retrying": 0,
        "exhausted": 0,
        "abandoned": 1,
        "status": "degraded",
    }
    monkeypatch.setattr(native_file_projection, "pending_stats", _async_return(diagnostic))

    result = await health_app.health(user=None)

    assert result["native_file_projection"] == diagnostic


async def test_vault_health_surfaces_native_file_projection_diagnostics(monkeypatch):
    from app.services import embed_worker, health as health_service, metadata_worker, native_file_projection

    vault_id = uuid.uuid4()
    diagnostic = {
        "pending": 1,
        "retrying": 0,
        "exhausted": 1,
        "abandoned": 0,
        "status": "degraded",
    }
    monkeypatch.setattr(embed_worker, "pending_stats", _async_return({"upsert": {}}))
    monkeypatch.setattr(metadata_worker, "pending_stats", _async_return({}))
    monkeypatch.setattr(native_file_projection, "pending_stats", _async_return(diagnostic))

    result = await health_service.vault_health(vault_id)

    assert result["native_file_projection"] == diagnostic


# ── the socket itself ────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_the_listener_really_binds_a_second_socket_in_this_process(db, monkeypatch):
    """The central claim of the design: a second HTTP surface, same process.

    Everything above tests the handler function directly; this is the only test
    that proves the port exists, that it answers, and that it is separate from
    the API app — a request for an API path gets a 404 here.
    """
    monkeypatch.delenv(listener.PORT_ENV_VAR, raising=False)
    monkeypatch.setattr(settings.stats, "host", "127.0.0.1")
    monkeypatch.setattr(settings.stats, "port", _free_port())

    assert listener.start() is True
    try:
        for _ in range(200):
            if listener._server is not None and listener._server.started:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("stats listener did not start")

        base = f"http://127.0.0.1:{settings.stats.port}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            before = await client.get(f"{base}/stats")
            assert before.status_code == 503

            await sampler.sample_once()
            after = await client.get(f"{base}/stats")
            assert after.status_code == 200
            jsonschema.validate(after.json(), SCHEMA)

            # Not the API app: nothing else is reachable on this port.
            assert (await client.get(f"{base}/health")).status_code == 404
            assert (await client.get(f"{base}/api/v1/vaults")).status_code == 404
    finally:
        await listener.stop(timeout=5.0)

    assert listener.is_running() is False


async def test_a_port_that_cannot_be_bound_stops_the_boot(monkeypatch):
    """A configured port that will not bind must fail `start()`, loudly.

    Binding inside the serving task would let `start()` return True while the
    task died on EADDRINUSE — a port misrendered onto one already in use would
    then produce a pod that passes every probe and has no stats socket, which
    the platform only discovers as connection errors on its poller. uvicorn
    reports this by calling `sys.exit(1)`, so the check also proves the
    SystemExit is converted rather than left to unwind the whole boot.
    """
    monkeypatch.delenv(listener.PORT_ENV_VAR, raising=False)
    monkeypatch.setattr(settings.stats, "host", "127.0.0.1")

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        monkeypatch.setattr(settings.stats, "port", port)

        with pytest.raises(RuntimeError, match=str(port)):
            listener.start()

    # Nothing half-composed: a later start() is not confused by the failure,
    # and stop() has nothing to do.
    assert listener.is_running() is False
    assert listener._task is None
    assert listener._socket is None
    await asyncio.wait_for(listener.stop(), timeout=2.0)


async def test_a_serving_task_that_dies_after_the_bind_is_logged(monkeypatch, caplog):
    """`start()` returned True, so nothing upstream will look again.

    A task that raises after the bind — uvicorn's startup, a protocol error —
    would otherwise just finish: `is_running()` turns False, `stop()` reads a
    finished task as nothing to drain, and no line anywhere says the port
    stopped answering.
    """

    async def _dies(self, sockets=None):
        raise RuntimeError("serve exploded")

    monkeypatch.setattr(listener._NoSignalServer, "serve", _dies)
    monkeypatch.delenv(listener.PORT_ENV_VAR, raising=False)
    monkeypatch.setattr(settings.stats, "host", "127.0.0.1")
    monkeypatch.setattr(settings.stats, "port", _free_port())

    with caplog.at_level(logging.ERROR, logger="akb.stats_listener"):
        assert listener.start() is True
        sock = listener._socket
        for _ in range(50):
            if not listener.is_running():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the serving task did not finish")
        await asyncio.sleep(0)  # the done callback runs on the next loop turn

    died = [r for r in caplog.records if "stopped serving" in r.getMessage()]
    assert died and died[0].exc_info and "serve exploded" in repr(died[0].exc_info[1])
    # The failure is not swallowed at stop() time either: the socket is released.
    await listener.stop(timeout=1.0)
    assert sock is not None and sock.fileno() == -1


@pytest.mark.parametrize("drain_timeout", [5.0, 0.0], ids=["graceful", "cancelled"])
async def test_stopping_closes_the_listening_socket(monkeypatch, drain_timeout):
    """`stop()` leaves no listening socket open, on either drain path.

    A zero budget takes the cancel path, where uvicorn's graceful shutdown —
    the thing that closes the sockets it was handed — never runs.

    This pins the property, not the line that provides it: today uvicorn closes
    the socket on the graceful path and the `asyncio.Server` wrapping the fd
    closes it on the cancel path, so removing `stop()`'s own `close()` still
    passes. It is a regression guard for the property that the port is free
    once `stop()` returns, which is what the next process to bind it depends
    on. Asserted on the socket object because rebinding the port would pass on
    a leak too — the fd would be released by the collector moments later.
    """
    monkeypatch.delenv(listener.PORT_ENV_VAR, raising=False)
    monkeypatch.setattr(settings.stats, "host", "127.0.0.1")
    monkeypatch.setattr(settings.stats, "port", _free_port())

    assert listener.start() is True
    sock = listener._socket
    assert sock is not None and sock.fileno() != -1

    await listener.stop(timeout=drain_timeout)

    assert sock.fileno() == -1, "the listening socket outlived stop()"
