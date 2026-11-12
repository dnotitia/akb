"""Readiness must not turn a saturated request pool into an outage.

Every replica sees the same client burst at the same moment. Readiness used to
ping the database with a connection borrowed from the request pool, so a pool
that requests had fully checked out timed the probe out on every replica at
once and left the Service with no endpoints while the database was fine. The
ping now runs on a dedicated connection and the pool is only observed: it
fails readiness after it has stayed saturated for a grace period.

These tests need no database; `test_readiness_probe_postgres.py` proves the
same behaviour against a real one.
"""

from __future__ import annotations

import asyncio
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from app.config import Settings
from app.db import postgres

GRACE = 30.0


class _Pool:
    """The occupancy reads readiness uses, and the one borrow it may make: an
    idle connection, when the server has refused the probe's own."""

    def __init__(self, size: int, max_size: int, idle: int) -> None:
        self.size, self.max_size, self.idle = size, max_size, idle
        self.closing = False
        self.idle_conn = SimpleNamespace(fetchval=AsyncMock(return_value=1), terminate=MagicMock())
        self.acquire_timeouts: list[float | None] = []
        self.release_timeouts: list[float | None] = []
        # How opening a connection goes when nothing is idle: refused, or stalled.
        self.acquire_error: BaseException | None = None
        self.acquire_delay = 0.0
        # Awaited on each completed acquire, as asyncpg awaits the pool's hook.
        self.setup = None

    def get_size(self) -> int:
        return self.size

    def get_max_size(self) -> int:
        return self.max_size

    def get_idle_size(self) -> int:
        return self.idle

    def is_closing(self) -> bool:
        return self.closing

    async def acquire(self, *, timeout=None):
        self.acquire_timeouts.append(timeout)
        if self.acquire_delay:
            await asyncio.sleep(self.acquire_delay)
        if self.acquire_error is not None:
            raise self.acquire_error
        if self.setup is not None:
            await self.setup(self.idle_conn)
        return self.idle_conn

    async def release(self, conn, *, timeout=None) -> None:
        assert conn is self.idle_conn
        self.release_timeouts.append(timeout)

    async def fetchval(self, *_args):  # pragma: no cover — a failure if ever reached
        raise AssertionError("readiness must not borrow a request-pool connection")


class _Clock:
    """Stands in for `time` inside app.main only; the event loop keeps its clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


class _Progress:
    """Stands in for `pool_progress`, on the same fake clock as readiness."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.total = 0
        self.last_at: float | None = None

    def acquire(self) -> None:
        self.total += 1
        self.last_at = self.clock.now

    def read(self) -> tuple[int, float | None]:
        return self.total, None if self.last_at is None else self.clock.now - self.last_at


@pytest.fixture
def main(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app import main as application

    monkeypatch.setattr(application, "_ready_state", application._ReadyState())
    monkeypatch.setattr(application, "_ready_lock", asyncio.Lock())
    monkeypatch.setattr(application.settings, "readyz_pool_saturation_grace_secs", GRACE)
    return application


@pytest.fixture
def probe(main, monkeypatch):
    """A probe whose ping succeeds, over a pool the test controls, on a fake clock.

    One acquire completes at the start; after that the pool makes progress
    only when a test calls `progress.acquire()`.
    """
    pool = _Pool(size=2, max_size=4, idle=1)
    ping = AsyncMock(return_value=None)
    store = SimpleNamespace(health=AsyncMock(return_value=True))
    clock = _Clock()
    progress = _Progress(clock)
    progress.acquire()
    monkeypatch.setattr(main, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(main, "readiness_ping", ping)
    monkeypatch.setattr(main, "get_vector_store", lambda: store)
    monkeypatch.setattr(main, "time", clock)
    monkeypatch.setattr(main, "pool_progress", progress.read)
    return SimpleNamespace(pool=pool, ping=ping, store=store, clock=clock, progress=progress)


def _saturate(pool: _Pool) -> None:
    pool.size, pool.idle = pool.max_size, 0


# ── Setting ──────────────────────────────────────────────────────────────────


def test_saturation_grace_defaults_to_thirty_seconds_and_rejects_negative():
    assert Settings(document_revision_backend="bare_git").readyz_pool_saturation_grace_secs == 30.0
    strict = Settings(document_revision_backend="bare_git", readyz_pool_saturation_grace_secs=0)
    assert strict.readyz_pool_saturation_grace_secs == 0.0
    with pytest.raises(ValueError):
        Settings(document_revision_backend="bare_git", readyz_pool_saturation_grace_secs=-1)


# ── Pool observation and the grace ───────────────────────────────────────────


async def test_pool_with_room_is_ready_and_reports_occupancy(main, probe):
    ok, detail = await main._probe_ready()

    assert ok is True
    assert detail["db"] == "ok"
    assert detail["pool"] == {
        "size": 2, "max": 4, "idle": 1, "free": 1, "saturated_for_secs": None,
        "last_acquire_age_secs": 0.0,
    }
    assert detail["vector_store"] == "ok"
    probe.ping.assert_awaited_once()


async def test_full_pool_that_can_still_grow_is_not_saturated(main, probe):
    probe.pool.size, probe.pool.idle = 3, 0  # every open connection lent, room for one more

    ok, detail = await main._probe_ready()

    assert ok is True
    assert detail["pool"]["saturated_for_secs"] is None


async def test_saturated_pool_within_grace_is_ready_and_skips_the_vector_check(main, probe):
    _saturate(probe.pool)

    ok, detail = await main._probe_ready()
    assert ok is True
    assert detail["db"] == "ok"
    assert detail["pool"] == {
        "size": 4, "max": 4, "idle": 0, "free": 0, "saturated_for_secs": 0.0,
        "last_acquire_age_secs": 0.0,
    }
    # pgvector on the main database borrows from this same pool for its health
    # check, so running it here would queue behind the saturation.
    assert detail["vector_store"] == "skipped:pool_saturated"
    probe.store.health.assert_not_awaited()

    probe.clock.now += GRACE - 0.5
    ok, detail = await main._probe_ready()
    assert ok is True
    assert detail["pool"]["saturated_for_secs"] == GRACE - 0.5


async def test_saturation_that_lasts_the_grace_without_progress_fails_readiness(main, probe):
    _saturate(probe.pool)
    assert (await main._probe_ready())[0] is True

    probe.clock.now += GRACE  # no acquire completes: the holders never let go
    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["db"] == "ok"
    assert detail["pool"]["saturated_for_secs"] == GRACE
    assert detail["pool"]["last_acquire_age_secs"] == GRACE
    assert "vector_store" not in detail
    probe.store.health.assert_not_awaited()


async def test_saturated_pool_that_keeps_completing_acquires_stays_ready(main, probe):
    """Busy, not wedged: every connection is lent out, but they change hands."""
    _saturate(probe.pool)
    assert (await main._probe_ready())[0] is True
    for _ in range(4):
        probe.clock.now += GRACE
        probe.progress.acquire()
        ok, detail = await main._probe_ready()
        assert ok is True, detail
        assert detail["pool"]["last_acquire_age_secs"] == 0.0
    assert detail["pool"]["saturated_for_secs"] == 4 * GRACE
    assert probe.progress.total == 5

    # Once the connections stop changing hands, the grace runs out.
    probe.clock.now += GRACE
    ok, detail = await main._probe_ready()
    assert ok is False
    assert detail["pool"]["last_acquire_age_secs"] == GRACE


async def test_without_a_recorded_acquire_the_saturation_run_decides_alone(main, probe):
    probe.progress.last_at = None  # a pool built without the counting hook
    _saturate(probe.pool)
    assert (await main._probe_ready())[0] is True

    probe.clock.now += GRACE
    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["pool"]["last_acquire_age_secs"] is None


async def test_a_probe_that_finds_room_ends_the_saturation_run(main, probe):
    _saturate(probe.pool)
    await main._probe_ready()
    probe.clock.now += GRACE - 1

    probe.pool.idle = 1
    ok, detail = await main._probe_ready()
    assert ok is True
    assert detail["pool"]["saturated_for_secs"] is None
    assert main._ready_state.pool_saturated_since is None

    # Saturated again: the earlier run does not count towards the grace.
    probe.clock.now += 5
    _saturate(probe.pool)
    ok, detail = await main._probe_ready()
    assert ok is True
    assert detail["pool"]["saturated_for_secs"] == 0.0


async def test_values_just_below_the_grace_are_judged_unrounded(main, probe):
    # Both measures round to 30.0 in the payload, but neither has reached the
    # grace: the verdict must not round.
    _saturate(probe.pool)
    assert (await main._probe_ready())[0] is True

    probe.clock.now += GRACE - 0.0004  # no acquire since the start either

    ok, detail = await main._probe_ready()
    assert ok is True, detail
    assert detail["pool"]["saturated_for_secs"] == GRACE
    assert detail["pool"]["last_acquire_age_secs"] == GRACE


async def test_acquire_just_inside_the_grace_keeps_a_long_saturated_pool_ready(main, probe):
    _saturate(probe.pool)
    assert (await main._probe_ready())[0] is True
    probe.clock.now += 2 * GRACE
    probe.progress.last_at = probe.clock.now - (GRACE - 0.0004)

    ok, detail = await main._probe_ready()

    assert ok is True, detail
    assert detail["pool"]["saturated_for_secs"] == 2 * GRACE
    assert detail["pool"]["last_acquire_age_secs"] == GRACE  # rounded for display only


async def test_public_payload_keeps_the_acquire_count_internal(main, probe):
    ok, detail = await main._probe_ready()

    assert ok is True
    assert "acquires_total" not in detail["pool"]
    assert probe.progress.total == 1


async def test_zero_grace_fails_at_the_first_saturated_probe(main, probe, monkeypatch):
    monkeypatch.setattr(main.settings, "readyz_pool_saturation_grace_secs", 0.0)
    _saturate(probe.pool)

    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["pool"]["saturated_for_secs"] == 0.0


# ── A pool that cannot show it works ────────────────────────────────────────


def _unproven(probe) -> None:
    """Room to open a connection, none idle, and no acquire within the grace."""
    probe.pool.size, probe.pool.idle = 1, 0
    probe.clock.now += GRACE


async def test_unproven_pool_is_checked_with_one_bounded_acquire(main, probe):
    _unproven(probe)

    ok, detail = await main._probe_ready()

    assert ok is True
    assert detail["db"] == "ok"
    probe.pool.idle_conn.fetchval.assert_awaited_once_with("SELECT 1")
    (acquire_timeout,) = probe.pool.acquire_timeouts
    assert acquire_timeout is not None and 0 < acquire_timeout <= main._READY_DB_TIMEOUT_SECONDS
    assert len(probe.pool.release_timeouts) == 1


async def test_unproven_pool_that_the_server_refuses_is_not_ready(main, probe):
    # The probe's own connection, opened earlier, still answers; the pool
    # cannot open one, so requests could not be served.
    _unproven(probe)
    probe.pool.acquire_error = asyncpg.TooManyConnectionsError("sorry, too many clients already")

    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["db"] == "error:pool_unavailable"
    assert detail["pool"]["size"] == 1 and detail["pool"]["idle"] == 0
    probe.store.health.assert_not_awaited()


async def test_unproven_pool_that_cannot_connect_in_time_is_not_ready_within_the_budget(main, probe):
    _unproven(probe)
    probe.pool.acquire_delay = 60

    started = time.monotonic()
    ok, detail = await asyncio.wait_for(main._probe_ready(), timeout=3)
    elapsed = time.monotonic() - started

    assert ok is False
    assert detail["db"] == "error:pool_timeout"
    assert elapsed < main._READY_DB_TIMEOUT_SECONDS + 0.5


async def test_probe_waiting_for_a_connection_stops_when_a_request_gets_one(main, probe, monkeypatch):
    # A request has taken the last holder and is still opening its connection;
    # the probe queues behind it. When that request's acquire completes, the
    # pool has served it: the probe stops waiting instead of timing out.
    monkeypatch.setattr(postgres, "_acquires_total", 0)
    monkeypatch.setattr(postgres, "_last_acquire_at", None)
    _unproven(probe)
    probe.pool.acquire_delay = 60

    async def request_connects_after(delay: float) -> None:
        await asyncio.sleep(delay)
        await postgres._count_acquire(object())  # the pool's hook, for the request

    request = asyncio.create_task(request_connects_after(0.05))
    started = time.monotonic()
    ok, detail = await main._probe_ready()
    elapsed = time.monotonic() - started
    await request

    assert ok is True, detail
    assert detail["db"] == "ok"
    assert elapsed < 1.0
    probe.pool.idle_conn.fetchval.assert_not_awaited()
    assert postgres._progress_waiters == set()


@pytest.mark.parametrize("evidence", ["idle", "recent_acquire", "saturated"])
async def test_pool_that_shows_it_works_is_not_borrowed_from(main, probe, evidence):
    _unproven(probe)
    if evidence == "idle":
        probe.pool.idle = 1
    elif evidence == "recent_acquire":
        probe.progress.acquire()
    else:
        probe.pool.size = probe.pool.max_size  # judged by the wedge rule instead

    ok, detail = await main._probe_ready()

    assert ok is True
    assert detail["db"] == "ok"
    assert probe.pool.acquire_timeouts == []


# ── /readyz cache ────────────────────────────────────────────────────────────


async def test_ready_result_with_room_is_cached(main, probe):
    assert (await main.readyz())["cached"] is False
    assert (await main.readyz())["cached"] is True
    probe.ping.assert_awaited_once()


def _past_the_floor(main, probe) -> None:
    probe.clock.now += main._READY_MIN_INTERVAL_SECONDS


async def test_ready_result_during_saturation_is_shared_only_for_the_floor(main, probe):
    _saturate(probe.pool)

    first = await main.readyz()
    second = await main.readyz()
    _past_the_floor(main, probe)
    third = await main.readyz()

    assert first["cached"] is False
    assert second["cached"] is True
    assert third["cached"] is False
    assert probe.ping.await_count == 2


async def test_concurrent_callers_share_one_evaluation(main, probe):
    # /readyz needs no authentication: a crowd of callers during saturation,
    # when nothing is cached for long, must still cost one evaluation.
    async def slow_ping(**_kwargs):
        await asyncio.sleep(0.05)

    probe.ping.side_effect = slow_ping
    _saturate(probe.pool)

    results = await asyncio.gather(*(main.readyz() for _ in range(50)))

    assert probe.ping.await_count == 1
    assert all(result["status"] == "ready" for result in results)
    assert sum(result["cached"] for result in results) == 49


async def test_failure_is_shared_for_the_floor_then_probed_again(main, probe):
    probe.ping.side_effect = ConnectionRefusedError("refused")
    for _ in range(2):
        with pytest.raises(main.HTTPException):
            await main.readyz()
    assert probe.ping.await_count == 1

    probe.ping.side_effect = None
    _past_the_floor(main, probe)
    assert (await main.readyz())["status"] == "ready"
    assert probe.ping.await_count == 2


async def test_readyz_answers_503_once_saturation_outlasts_the_grace(main, probe):
    _saturate(probe.pool)
    assert (await main.readyz())["status"] == "ready"

    probe.clock.now += GRACE + 1
    with pytest.raises(main.HTTPException) as caught:
        await main.readyz()
    assert caught.value.status_code == 503
    assert caught.value.detail["status"] == "not_ready"
    assert caught.value.detail["detail"]["pool"]["saturated_for_secs"] == GRACE + 1

    probe.pool.idle = 1  # the pool drains; a failure is shared only for the floor
    _past_the_floor(main, probe)
    recovered = await main.readyz()
    assert recovered["status"] == "ready"
    assert recovered["detail"]["pool"]["saturated_for_secs"] is None


# ── Database failures ────────────────────────────────────────────────────────


async def test_ping_failure_is_not_ready_and_still_reports_the_pool(main, probe):
    probe.ping.side_effect = ConnectionRefusedError("private-host:5432")
    _saturate(probe.pool)

    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["db"] == "error:unavailable"
    assert detail["pool"]["idle"] == 0
    assert "private-host" not in str(detail)
    probe.store.health.assert_not_awaited()


_SERVER_FULL = asyncpg.TooManyConnectionsError("sorry, too many clients already")


async def test_server_at_max_connections_pings_through_an_idle_pool_connection(main, probe):
    probe.ping.side_effect = _SERVER_FULL
    probe.clock.now += 2 * GRACE  # no acquire within the grace: progress cannot vouch

    ok, detail = await main._probe_ready()

    assert ok is True
    assert detail["db"] == "ok:pool_idle"
    probe.pool.idle_conn.fetchval.assert_awaited_once_with("SELECT 1")
    # Borrowed and given back within the rest of the probe's budget, no longer.
    (acquire_timeout,) = probe.pool.acquire_timeouts
    (release_timeout,) = probe.pool.release_timeouts
    assert acquire_timeout is not None and 0 < acquire_timeout <= main._READY_DB_TIMEOUT_SECONDS
    assert release_timeout is not None and 0 < release_timeout <= acquire_timeout
    probe.pool.idle_conn.terminate.assert_not_called()
    assert detail["vector_store"] == "ok"


async def test_ready_on_an_idle_pool_connection_is_shared_only_for_the_floor(main, probe):
    probe.ping.side_effect = _SERVER_FULL

    first = await main.readyz()
    _past_the_floor(main, probe)
    second = await main.readyz()

    assert first["detail"]["db"] == second["detail"]["db"] == "ok:pool_idle"
    assert first["cached"] is False and second["cached"] is False
    assert len(probe.pool.acquire_timeouts) == 2


async def test_failed_ping_on_the_idle_pool_connection_is_not_ready(main, probe):
    probe.ping.side_effect = _SERVER_FULL
    probe.pool.idle_conn.fetchval.side_effect = asyncpg.ConnectionDoesNotExistError("gone")

    ok, detail = await main._probe_ready()

    # Recent progress does not override a ping that failed on the pool itself.
    assert ok is False
    assert detail["db"] == "error:unavailable"
    # Given up rather than returned: returning it could wait on a cancel.
    probe.pool.idle_conn.terminate.assert_called_once()
    assert probe.pool.release_timeouts == []


async def test_hung_ping_on_the_idle_pool_connection_is_cut_off_inside_the_probe_budget(main, probe):
    async def hang(*_args):
        await asyncio.sleep(60)

    probe.ping.side_effect = _SERVER_FULL
    probe.pool.idle_conn.fetchval.side_effect = hang

    started = time.monotonic()
    ok, detail = await asyncio.wait_for(main._probe_ready(), timeout=3)
    elapsed = time.monotonic() - started

    assert ok is False
    assert detail["db"] == "error:timeout"
    assert elapsed < main._READY_DB_TIMEOUT_SECONDS + 0.5
    probe.pool.idle_conn.terminate.assert_called_once()
    assert probe.pool.release_timeouts == []


async def test_pool_ping_that_uses_up_the_budget_gives_the_connection_up(main):
    pool = _Pool(size=1, max_size=1, idle=1)
    loop = asyncio.get_running_loop()
    # The ping answers, but only once the probe's budget is already gone.
    deadlines = iter([loop.time() + 1.0, loop.time() - 0.1])
    budget = SimpleNamespace(when=lambda: next(deadlines))

    with pytest.raises(TimeoutError):
        await main._ping_through_pool(pool, budget)

    pool.idle_conn.fetchval.assert_awaited_once_with("SELECT 1")
    pool.idle_conn.terminate.assert_called_once()
    assert pool.release_timeouts == []


def _no_idle(pool: _Pool) -> None:
    pool.idle = 0  # every open connection is lent out: nothing to borrow


async def test_server_at_max_connections_without_an_idle_connection_relies_on_recent_progress(main, probe):
    _no_idle(probe.pool)
    probe.ping.side_effect = _SERVER_FULL
    probe.clock.now += GRACE - 1  # the latest acquire completed GRACE - 1 seconds ago

    ok, detail = await main._probe_ready()

    assert ok is True
    assert detail["db"] == "ok:pool_progress"
    assert probe.pool.acquire_timeouts == []
    assert detail["vector_store"] == "ok"


async def test_ready_on_pool_progress_is_shared_only_for_the_floor(main, probe):
    _no_idle(probe.pool)
    probe.ping.side_effect = _SERVER_FULL

    first = await main.readyz()
    _past_the_floor(main, probe)
    second = await main.readyz()

    assert first["detail"]["db"] == second["detail"]["db"] == "ok:pool_progress"
    assert first["cached"] is False and second["cached"] is False
    assert probe.ping.await_count == 2


async def test_server_at_max_connections_without_idle_or_recent_progress_is_not_ready(main, probe):
    _no_idle(probe.pool)
    probe.ping.side_effect = _SERVER_FULL
    probe.clock.now += GRACE

    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["db"] == "error:unavailable"
    probe.store.health.assert_not_awaited()


async def test_server_at_max_connections_without_idle_or_any_recorded_acquire_is_not_ready(main, probe):
    _no_idle(probe.pool)
    probe.progress.last_at = None
    probe.ping.side_effect = _SERVER_FULL

    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["db"] == "error:unavailable"


@pytest.mark.parametrize("idle", [0, 1])
async def test_closing_pool_does_not_vouch_for_the_database(main, probe, idle):
    probe.pool.idle = idle
    probe.pool.closing = True
    probe.ping.side_effect = _SERVER_FULL

    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["db"] == "error:unavailable"
    assert probe.pool.acquire_timeouts == []


@pytest.mark.parametrize("failure", [
    ConnectionRefusedError("refused"),
    OSError("unreachable"),
    asyncpg.InvalidPasswordError("authentication failed"),
    asyncpg.InvalidCatalogNameError("database does not exist"),
    asyncpg.CannotConnectNowError("the database system is starting up"),
])
async def test_other_connect_errors_stay_not_ready_despite_an_idle_pool_and_progress(main, probe, failure):
    probe.ping.side_effect = failure  # one idle connection; the latest acquire is 0 s old

    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["db"] == "error:unavailable"
    assert probe.pool.acquire_timeouts == []


async def test_hung_ping_is_cut_off_inside_the_probe_budget(main, probe):
    cancelled = asyncio.Event()

    async def hung(**_kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    probe.ping.side_effect = hung

    ok, detail = await asyncio.wait_for(main._probe_ready(), timeout=3)

    assert ok is False
    assert detail["db"] == "error:timeout"
    assert cancelled.is_set()


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _settings_at(port: int) -> Settings:
    return Settings(
        document_revision_backend="bare_git",
        db_host="127.0.0.1",
        db_port=port,
        db_user="akb",
        db_password="fixture-password",  # pragma: allowlist secret
    )


@pytest.fixture
async def real_ping(main, monkeypatch):
    """The real `readiness_ping`, against a database address the test chooses."""
    monkeypatch.setattr(main, "get_pool", AsyncMock(return_value=_Pool(size=1, max_size=4, idle=1)))
    yield monkeypatch
    await postgres.close_readiness_connection()


async def test_unreachable_database_is_not_ready_at_once(main, real_ping):
    real_ping.setattr(postgres, "settings", _settings_at(_closed_port()))

    started = time.monotonic()
    ok, detail = await main._probe_ready()

    assert ok is False
    assert detail["db"] == "error:unavailable"
    assert time.monotonic() - started < 1.0
    assert "127.0.0.1" not in str(detail) and "fixture-password" not in str(detail)


async def test_silent_database_is_not_ready_within_the_probe_budget(main, real_ping):
    async def silent(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_ping.setattr(postgres, "settings", _settings_at(port))
    try:
        started = time.monotonic()
        ok, detail = await main._probe_ready()
        elapsed = time.monotonic() - started
    finally:
        server.close()
        await server.wait_closed()

    assert ok is False
    assert detail["db"] == "error:timeout"
    assert elapsed < main._READY_DB_TIMEOUT_SECONDS + 1.0


# ── The dedicated probe connection ───────────────────────────────────────────


class _Conn:
    def __init__(self, fetchval=None) -> None:
        self.closed = False
        self.terminated = False
        self.fetchval = fetchval or AsyncMock(return_value=1)

    def is_closed(self) -> bool:
        return self.closed or self.terminated

    def terminate(self) -> None:
        self.terminated = True

    async def close(self, *, timeout=None) -> None:
        self.closed = True


@pytest.fixture
def connections(monkeypatch):
    """Replace asyncpg.connect; record each call and hand out queued connections."""
    calls: list[dict] = []
    queue: list[_Conn] = []
    made: list[_Conn] = []

    async def connect(**kwargs):
        calls.append(kwargs)
        conn = queue.pop(0) if queue else _Conn()
        made.append(conn)
        return conn

    monkeypatch.setattr(postgres.asyncpg, "connect", connect)
    monkeypatch.setattr(postgres, "_readiness_conn", None)
    return SimpleNamespace(calls=calls, queue=queue, made=made)


async def test_probe_connection_uses_the_pool_dsn_and_the_probe_budget(connections):
    await postgres.readiness_ping(timeout=2.0)
    await postgres.readiness_ping(timeout=2.0)

    assert len(connections.calls) == 1, "the connection is reused across probes"
    (call,) = connections.calls
    assert call["dsn"] == postgres.settings.asyncpg_dsn
    assert call["timeout"] == 2.0
    assert call["command_timeout"] == 2.0
    assert call["server_settings"] == {"application_name": "akb-readiness-probe"}
    assert connections.made[0].fetchval.await_count == 2


async def test_failed_ping_discards_the_connection_and_the_next_probe_reopens(connections):
    broken = _Conn(fetchval=AsyncMock(side_effect=asyncpg.ConnectionDoesNotExistError("gone")))
    connections.queue.append(broken)

    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        await postgres.readiness_ping(timeout=2.0)
    assert broken.terminated
    assert postgres._readiness_conn is None

    await postgres.readiness_ping(timeout=2.0)
    assert len(connections.calls) == 2
    assert postgres._readiness_conn is connections.made[1]


async def test_connection_the_server_closed_is_replaced_without_a_failed_probe(connections):
    await postgres.readiness_ping(timeout=2.0)
    connections.made[0].closed = True  # e.g. ended by a server restart while idle

    await postgres.readiness_ping(timeout=2.0)

    assert len(connections.calls) == 2
    assert connections.made[1].fetchval.await_count == 1


async def test_ping_cut_off_by_the_deadline_discards_the_connection(connections):
    async def hang(*_args):
        await asyncio.sleep(60)

    stuck = _Conn(fetchval=AsyncMock(side_effect=hang))
    connections.queue.append(stuck)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await postgres.readiness_ping(timeout=2.0)

    assert stuck.terminated
    assert postgres._readiness_conn is None


async def test_close_readiness_connection_closes_and_forgets_it(connections):
    await postgres.readiness_ping(timeout=2.0)
    conn = connections.made[0]

    await postgres.close_readiness_connection()

    assert conn.closed
    assert postgres._readiness_conn is None
    await postgres.close_readiness_connection()  # nothing open: a no-op


# ── Pool progress ────────────────────────────────────────────────────────────


async def test_pool_progress_counts_each_completed_acquire(monkeypatch):
    monkeypatch.setattr(postgres, "_acquires_total", 0)
    monkeypatch.setattr(postgres, "_last_acquire_at", None)
    assert postgres.pool_progress() == (0, None)

    await postgres._count_acquire(object())
    await postgres._count_acquire(object())

    total, age = postgres.pool_progress()
    assert total == 2
    assert age is not None and 0.0 <= age < 1.0


async def test_acquires_made_by_readiness_do_not_count_as_progress(monkeypatch):
    monkeypatch.setattr(postgres, "_acquires_total", 0)
    monkeypatch.setattr(postgres, "_last_acquire_at", None)

    with postgres.readiness_acquires():
        await postgres._count_acquire(object())
        # A task started inside the block inherits the marker.
        await asyncio.create_task(postgres._count_acquire(object()))
    assert postgres.pool_progress() == (0, None)

    await postgres._count_acquire(object())
    assert postgres.pool_progress()[0] == 1


@pytest.fixture
def real_progress(main, probe, monkeypatch):
    """The real acquire counter behind readiness, its latest acquire long past."""
    monkeypatch.setattr(main, "pool_progress", postgres.pool_progress)
    monkeypatch.setattr(postgres, "_acquires_total", 7)
    monkeypatch.setattr(postgres, "_last_acquire_at", time.monotonic() - 10 * GRACE)
    probe.pool.setup = postgres._count_acquire
    return probe


async def test_failed_pool_check_leaves_the_pool_unproven_for_the_next_evaluation(main, real_progress):
    probe = real_progress
    probe.pool.size, probe.pool.idle = 1, 0
    probe.pool.idle_conn.fetchval.side_effect = asyncpg.ConnectionDoesNotExistError("gone")

    for attempt in (1, 2):
        ok, detail = await main._probe_ready()
        assert ok is False
        assert detail["db"] == "error:pool_unavailable"
        # The probe's own acquire is not progress, so the next one checks again.
        assert len(probe.pool.acquire_timeouts) == attempt
    assert postgres.pool_progress()[0] == 7
    assert postgres.pool_progress()[1] >= 10 * GRACE - 1


async def test_probe_acquires_do_not_satisfy_the_max_connections_progress_rule(main, real_progress):
    probe = real_progress
    probe.ping.side_effect = _SERVER_FULL

    ok, detail = await main._probe_ready()  # pings through the idle connection
    assert ok is True
    assert detail["db"] == "ok:pool_idle"

    probe.pool.idle = 0
    ok, detail = await main._probe_ready()
    assert ok is False
    assert detail["db"] == "error:unavailable"


async def test_request_pool_counts_acquires_through_its_setup_hook(monkeypatch):
    created: list[dict] = []

    async def create_pool(**kwargs):
        created.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(postgres.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(postgres, "_pool", None)
    monkeypatch.delenv("AKB_PG_POOL_MIN_SIZE", raising=False)
    monkeypatch.delenv("AKB_PG_POOL_MAX_SIZE", raising=False)

    await postgres.get_pool()

    (kwargs,) = created
    assert kwargs["setup"] is postgres._count_acquire
    # Nothing else hooks this pool's connections; the counter replaces nothing.
    assert "init" not in kwargs and "reset" not in kwargs


async def test_shutdown_closes_the_probe_connection_before_the_pool(monkeypatch):
    from app.services import lifecycle

    order: list[str] = []

    async def record(name: str) -> None:
        order.append(name)

    monkeypatch.setattr(lifecycle.settings, "keycloak_enabled", False)
    monkeypatch.setattr(lifecycle.http_pool, "close_client", lambda: record("http_client"))
    monkeypatch.setattr(lifecycle, "close_readiness_connection", lambda: record("readiness_connection"))
    monkeypatch.setattr(lifecycle, "close_pool", lambda: record("pool"))

    await lifecycle.shutdown_storage()

    assert order == ["http_client", "readiness_connection", "pool"]
