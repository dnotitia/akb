"""Real-PostgreSQL proof that a saturated request pool does not fail readiness.

A client burst can check out every connection in the request pool on every
replica at once. Readiness used to queue its `SELECT 1` behind that, time out
after two seconds and answer 503 on every replica together, which left the
Service with no endpoints while the database was fine. Here the pool is
really saturated, its connections really keep changing hands or really stay
held, the probe connection is really terminated or refused, and the database
really disappears.

The pool is built by `get_pool`, as in production, so it carries the hook
that counts completed acquires. Each test owns a disposable database. Set
AKB_TEST_DSN to a test-cluster admin DSN; with REQUIRE_REAL_PG=1 an
unreachable server is a failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import asyncpg
import httpx
import pytest

from app.config import Settings
from app.db import postgres

pytestmark = pytest.mark.asyncio

_ADMIN_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_POOL_SIZE = 2
# Short enough to wait out in real time: the progress signal is measured on
# the real clock, so these tests do not fake time.
_GRACE = 1.0
_PAST_GRACE = _GRACE + 0.3


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_ADMIN_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _server() -> tuple[str, int]:
    parsed = urlsplit(_ADMIN_DSN)
    return parsed.hostname or "localhost", parsed.port or 5432


def _settings(
    name: str,
    *,
    user: str | None = None,
    password: str | None = None,
    pool_size: int = _POOL_SIZE,
    address: tuple[str, int] | None = None,
) -> Settings:
    parsed = urlsplit(_ADMIN_DSN)
    host, port = address or _server()
    return Settings(
        document_revision_backend="bare_git",
        db_host=host,
        db_port=port,
        db_name=name,
        db_user=user or unquote(parsed.username or "akb"),
        db_password=password if password is not None else unquote(parsed.password or ""),
        pg_pool_min_size=pool_size,
        pg_pool_max_size=pool_size,
    )


class _Relay:
    """A TCP relay to PostgreSQL that can go silent, as a stalled network path does.

    `freeze()` silences every connection made so far in both directions, and
    holds new ones (such as asyncpg's cancel request) without ever reaching the
    server. `accept_new()` lets new connections through again; the silenced
    ones stay silent. Every socket stays open until the relay closes.
    """

    def __init__(self) -> None:
        self.address: tuple[str, int] = ("127.0.0.1", 0)
        # How long a new connection takes to reach the server.
        self.connect_delay = 0.0
        self._accepted = 0
        self._silent_below = 0
        self._holding_new = False
        self._closing = asyncio.Event()
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.address = ("127.0.0.1", self._server.sockets[0].getsockname()[1])

    def freeze(self) -> None:
        self._silent_below = self._accepted
        self._holding_new = True

    def accept_new(self) -> None:
        self._holding_new = False

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        number = self._accepted
        self._accepted += 1
        self._writers.append(writer)
        if self._holding_new:
            await self._closing.wait()
            return
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        upstream_reader, upstream_writer = await asyncio.open_connection(*_server())
        self._writers.append(upstream_writer)
        await asyncio.gather(
            self._pipe(reader, upstream_writer, number), self._pipe(upstream_reader, writer, number),
        )

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, number: int) -> None:
        try:
            while data := await reader.read(65536):
                if number < self._silent_below:
                    await self._closing.wait()
                    return
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        # One side has ended (a server-side termination, say): end the other.
        writer.close()

    async def close(self) -> None:
        self._closing.set()
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def _pgvector_store(*, dsn: str | None = None):
    """The pgvector driver. With no DSN of its own, as deployed by default, it
    reads the main database through the main pool; its health checks are
    counted on `calls`."""
    from app.services.vector_store.pgvector import PgvectorStore

    store = PgvectorStore(
        dsn=dsn, schema="vector_index", dense_dim=8, sparse_shape="posting", get_main_pool=postgres.get_pool,
    )
    store.calls = 0
    checked = store.health

    async def counted_health(conn=None) -> bool:
        store.calls += 1
        return await (checked() if conn is None else checked(conn=conn))

    store.health = counted_health
    return store


@contextlib.asynccontextmanager
async def _ready_database(
    monkeypatch,
    tmp_path,
    *,
    role_connection_limit: int | None = None,
    pool_size: int = _POOL_SIZE,
    relay: _Relay | None = None,
    probe_relay: _Relay | None = None,
):
    """A disposable database, its request pool from `get_pool`, and an HTTP client.

    With `role_connection_limit`, the pool and the probe log in as a fresh role
    that the server lets open only that many connections. With `relay`, the
    pool reaches the server through it; with `probe_relay`, the probe's own
    connection does. Otherwise each connects directly.
    """
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"Required PostgreSQL is not reachable at {_ADMIN_DSN}")
        pytest.skip(f"PostgreSQL is not reachable at {_ADMIN_DSN}")

    from app.config import settings as process_settings

    monkeypatch.setattr(process_settings, "git_storage_path", str(tmp_path / "vaults"))
    from app import main

    # Another test module may have replaced `get_pool` on `app.main` before
    # this one runs; readiness must probe the real pool this test builds.
    monkeypatch.setattr(main, "get_pool", postgres.get_pool)

    admin = await asyncpg.connect(_ADMIN_DSN)
    name = f"akb_readyz_{uuid.uuid4().hex[:12]}"
    role = password = None
    pool = client = None
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        if role_connection_limit is not None:
            role, password = name, uuid.uuid4().hex
            await admin.execute(
                f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' CONNECTION LIMIT {role_connection_limit}"
            )
        monkeypatch.delenv("AKB_PG_POOL_MIN_SIZE", raising=False)
        monkeypatch.delenv("AKB_PG_POOL_MAX_SIZE", raising=False)
        monkeypatch.setattr(postgres, "_pool", None)
        monkeypatch.setattr(main, "_ready_state", main._ReadyState())
        monkeypatch.setattr(main, "_ready_lock", asyncio.Lock())
        monkeypatch.setattr(main, "_vector_check", None)
        store = _pgvector_store()
        monkeypatch.setattr(main, "get_vector_store", lambda: store)
        login = {"user": role, "password": password, "pool_size": pool_size}
        pool_address = relay.address if relay is not None else None
        monkeypatch.setattr(postgres, "settings", _settings(name, address=pool_address, **login))
        pool = await postgres.get_pool()
        probe_address = probe_relay.address if probe_relay is not None else None
        monkeypatch.setattr(postgres, "settings", _settings(name, address=probe_address, **login))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://akb.test")
        yield SimpleNamespace(main=main, admin=admin, name=name, pool=pool, store=store, client=client)
    finally:
        if client is not None:
            await client.aclose()
        await postgres.close_readiness_connection()
        if pool is not None:
            pool.terminate()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        if role is not None:
            await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


@pytest.fixture
async def ready_db(monkeypatch, tmp_path):
    async with _ready_database(monkeypatch, tmp_path) as db:
        yield db


@contextlib.asynccontextmanager
async def _every_connection_checked_out(pool: asyncpg.Pool):
    """Other tasks hold every connection the pool may open and never let go."""
    release = asyncio.Event()
    holding = 0

    async def hold() -> None:
        nonlocal holding
        async with pool.acquire():
            holding += 1
            await release.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(pool.get_max_size())]
    try:
        async with asyncio.timeout(5):
            while holding < pool.get_max_size():
                await asyncio.sleep(0.01)
        assert pool.get_size() == pool.get_max_size()
        assert pool.get_idle_size() == 0
        yield
    finally:
        release.set()
        await asyncio.gather(*tasks)


class _ChangingHands:
    """More tasks than connections take turns with every connection, briefly
    each, as a busy replica's requests do: all slots stay busy and acquires
    keep completing. `pause()` lets each holder keep its connection, so a
    probe observes the pool saturated rather than mid-handover."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self.holding = 0
        self._running = asyncio.Event()
        self._running.set()
        self._stopping = False
        self._tasks = [asyncio.create_task(self._turns()) for _ in range(2 * pool.get_max_size())]

    async def _turns(self) -> None:
        while not self._stopping:
            async with self.pool.acquire():
                self.holding += 1
                try:
                    await asyncio.sleep(0.005)
                    await self._running.wait()
                finally:
                    self.holding -= 1

    async def pause(self) -> None:
        self._running.clear()
        async with asyncio.timeout(5):
            while not (self.holding == self.pool.get_max_size() and self.pool.get_idle_size() == 0):
                await asyncio.sleep(0.005)

    def resume(self) -> None:
        self._running.set()

    async def stop(self) -> None:
        self._stopping = True
        self._running.set()
        await asyncio.gather(*self._tasks)


class _GatedConnect:
    """An asyncpg pool `connect` that opens the real connection, or meets the
    server's refusal, and then holds the outcome until `release()`. The test
    decides exactly when a connection the pool is opening becomes available."""

    def __init__(self) -> None:
        self.parked = 0
        self._gate = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        try:
            conn = await asyncpg.connect(*args, **kwargs)
        except Exception:
            self.parked += 1
            await self._gate.wait()
            raise
        self.parked += 1
        await self._gate.wait()
        return conn

    def release(self) -> None:
        self._gate.set()

    async def until_parked(self, count: int) -> None:
        async with asyncio.timeout(5):
            while self.parked < count:
                await asyncio.sleep(0.005)


async def _swap_in_a_gated_pool(db, monkeypatch, gated: _GatedConnect, *, max_size: int) -> asyncpg.Pool:
    """Replace the fixture's pool with an empty one that opens connections through `gated`."""
    db.pool.terminate()  # its connections would count against the role's limit
    pool = await asyncpg.create_pool(
        dsn=postgres.settings.asyncpg_dsn, min_size=0, max_size=max_size,
        connect=gated, setup=postgres._count_acquire,
    )
    monkeypatch.setattr(postgres, "_pool", pool)
    return pool


def _past_the_floor(main) -> None:
    """Age the last result past the floor during which every caller shares it."""
    main._ready_state.ts -= main._READY_MIN_INTERVAL_SECONDS


async def _probe_backend_pid(db) -> int | None:
    return await db.admin.fetchval(
        "SELECT pid FROM pg_stat_activity WHERE datname = $1 AND application_name = $2",
        db.name,
        postgres.READINESS_APPLICATION_NAME,
    )


async def test_saturated_pool_keeps_readyz_200_within_the_probe_budget(ready_db):
    async with _every_connection_checked_out(ready_db.pool):
        started = time.monotonic()
        response = await ready_db.client.get("/readyz")
        elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert elapsed < 2.0
    detail = response.json()["detail"]
    assert detail["db"] == "ok"
    assert detail["pool"]["size"] == detail["pool"]["max"] == _POOL_SIZE
    assert detail["pool"]["idle"] == 0
    assert detail["pool"]["saturated_for_secs"] is not None
    # The soft vector check would borrow from the same saturated pool.
    assert detail["vector_store"] == "skipped:pool_saturated"
    assert ready_db.store.calls == 0


async def test_fifty_concurrent_callers_during_saturation_share_one_evaluation(ready_db, monkeypatch):
    main = ready_db.main
    evaluations = 0
    probe_ready = main._probe_ready

    async def counted():
        nonlocal evaluations
        evaluations += 1
        return await probe_ready()

    monkeypatch.setattr(main, "_probe_ready", counted)
    async with _every_connection_checked_out(ready_db.pool):
        responses = await asyncio.gather(*(ready_db.client.get("/readyz") for _ in range(50)))

    assert [response.status_code for response in responses] == [200] * 50
    assert evaluations == 1


# This test's grace stays far above how long a probe can take after the
# holders pause, so a slow runner cannot age the last acquire past it. The
# time the connections spend changing hands is set separately, above it.
_CYCLING_GRACE = 2.0
_CYCLING_PERIOD = _CYCLING_GRACE + 0.5


async def _an_acquire_completes(after: int) -> None:
    """Wait until the pool has completed an acquire beyond `after`."""
    async with asyncio.timeout(5):
        while postgres.pool_progress()[0] <= after:
            await asyncio.sleep(0.005)


async def test_saturated_pool_whose_connections_keep_changing_hands_stays_200(ready_db, monkeypatch):
    monkeypatch.setattr(ready_db.main.settings, "readyz_pool_saturation_grace_secs", _CYCLING_GRACE)
    hands = _ChangingHands(ready_db.pool)
    try:
        await hands.pause()
        assert (await ready_db.client.get("/readyz")).status_code == 200
        for _ in range(2):
            hands.resume()
            await _an_acquire_completes(after=postgres.pool_progress()[0])
            await asyncio.sleep(_CYCLING_PERIOD)
            await hands.pause()
            response = await ready_db.client.get("/readyz")
            assert response.status_code == 200, response.text
            pool = response.json()["detail"]["pool"]
            assert pool["size"] == pool["max"] and pool["idle"] == 0
            assert pool["last_acquire_age_secs"] < _CYCLING_GRACE
            assert "acquires_total" not in pool  # the count stays internal
    finally:
        await hands.stop()

    # Saturated at every probe for well over the grace, and ready throughout,
    # because the connections kept changing hands in between.
    assert pool["saturated_for_secs"] >= 2 * _CYCLING_GRACE


async def test_saturated_pool_whose_holders_never_let_go_answers_503_and_recovers(ready_db, monkeypatch):
    monkeypatch.setattr(ready_db.main.settings, "readyz_pool_saturation_grace_secs", _GRACE)

    async with _every_connection_checked_out(ready_db.pool):
        first = await ready_db.client.get("/readyz")
        await asyncio.sleep(_PAST_GRACE)
        past = await ready_db.client.get("/readyz")

    assert first.status_code == 200, first.text
    assert past.status_code == 503, past.text
    body = past.json()["detail"]
    assert body["status"] == "not_ready"
    assert body["detail"]["db"] == "ok"
    assert body["detail"]["pool"]["saturated_for_secs"] >= _GRACE
    assert body["detail"]["pool"]["last_acquire_age_secs"] >= _GRACE

    # Released: once the failure's one-second floor has passed, the next probe
    # is ready again.
    _past_the_floor(ready_db.main)
    recovered = await ready_db.client.get("/readyz")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["detail"]["pool"]["saturated_for_secs"] is None
    assert recovered.json()["detail"]["vector_store"] == "ok"


# In the next two tests the role may open only the pool's own connections, so
# the server refuses the probe's with "too many connections" (SQLSTATE 53300)
# while the pool's connections keep working.


async def test_server_refusing_the_probe_connection_pings_through_an_idle_pool_connection(monkeypatch, tmp_path):
    async with _ready_database(monkeypatch, tmp_path, role_connection_limit=_POOL_SIZE) as db:
        monkeypatch.setattr(db.main.settings, "readyz_pool_saturation_grace_secs", _GRACE)
        await asyncio.sleep(_PAST_GRACE)  # an idle pool: no acquire within the grace
        assert db.pool.get_idle_size() == _POOL_SIZE
        _, last_acquire_age = postgres.pool_progress()
        assert last_acquire_age is None or last_acquire_age >= _GRACE

        first = await db.client.get("/readyz")
        _past_the_floor(db.main)
        second = await db.client.get("/readyz")

    assert first.status_code == 200, first.text
    assert first.json()["detail"]["db"] == "ok:pool_idle"
    # The soft vector check runs on the probe's own connection, which the
    # server refuses as well; it borrows nothing from the request pool.
    assert first.json()["detail"]["vector_store"] == "degraded:unavailable"
    # Not cached beyond the floor: the next probe asks the pool again.
    assert second.status_code == 200, second.text
    assert second.json()["cached"] is False


async def test_server_refusing_the_probe_connection_with_every_connection_held_relies_on_progress(
    monkeypatch, tmp_path,
):
    async with _ready_database(monkeypatch, tmp_path, role_connection_limit=_POOL_SIZE) as db:
        monkeypatch.setattr(db.main.settings, "readyz_pool_saturation_grace_secs", _GRACE)
        async with _every_connection_checked_out(db.pool):  # the holders' acquires just completed
            progressing = await db.client.get("/readyz")
            await asyncio.sleep(_PAST_GRACE)  # the holders never let go: no acquire since
            stalled = await db.client.get("/readyz")

    assert progressing.status_code == 200, progressing.text
    assert progressing.json()["detail"]["db"] == "ok:pool_progress"
    assert progressing.json()["cached"] is False
    assert stalled.status_code == 503, stalled.text
    assert stalled.json()["detail"]["detail"]["db"] == "error:unavailable"


async def test_idle_pool_connection_that_never_answers_is_given_up_within_the_probe_budget(monkeypatch, tmp_path):
    # The pool's one connection uses the role's one slot, so the server refuses
    # the probe's own connection and readiness pings through the pool. The
    # relay then drops that ping, and the cancel request asyncpg sends for it.
    relay = _Relay()
    await relay.start()
    try:
        async with _ready_database(
            monkeypatch, tmp_path, role_connection_limit=1, pool_size=1, relay=relay,
        ) as db:
            assert db.pool.get_idle_size() == 1
            relay.freeze()

            started = time.monotonic()
            response = await db.client.get("/readyz")
            elapsed = time.monotonic() - started
    finally:
        await relay.close()

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["detail"]["db"] == "error:timeout"
    # The whole database step, giving the connection up included, stays
    # within its budget instead of waiting on the cancel as long again.
    assert elapsed < db.main._READY_DB_TIMEOUT_SECONDS + 0.5


async def test_probe_behind_a_request_that_is_still_connecting_stays_ready(monkeypatch, tmp_path):
    # The pool's only connection has ended and nothing has acquired for longer
    # than the grace, so readiness checks the pool. A request has just taken the
    # only holder and is still opening its connection: the probe queues behind
    # it. Once the request's connection opens, the pool has served it.
    relay = _Relay()
    await relay.start()
    request = None
    release = asyncio.Event()
    try:
        async with _ready_database(monkeypatch, tmp_path, pool_size=1, relay=relay) as db:
            monkeypatch.setattr(db.main.settings, "readyz_pool_saturation_grace_secs", _GRACE)
            for row in await db.admin.fetch("SELECT pid FROM pg_stat_activity WHERE datname = $1", db.name):
                await db.admin.fetchval("SELECT pg_terminate_backend($1)", row["pid"])
            async with asyncio.timeout(2):
                while db.pool.get_size() > 0:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(_PAST_GRACE)
            relay.connect_delay = 0.5

            async def serve_a_request() -> None:
                async with db.pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                    await release.wait()

            request = asyncio.create_task(serve_a_request())
            await asyncio.sleep(0.05)
            assert db.pool.get_size() == 0 and db.pool.get_idle_size() == 0  # still connecting

            started = time.monotonic()
            response = await db.client.get("/readyz")
            elapsed = time.monotonic() - started
            release.set()
            await request
    finally:
        release.set()
        await relay.close()

    assert response.status_code == 200, response.text
    assert response.json()["detail"]["db"] == "ok"
    assert elapsed < db.main._READY_DB_TIMEOUT_SECONDS


def _connections_held(pool: asyncpg.Pool) -> int:
    return pool.get_size() - pool.get_idle_size()


async def _until_nothing_held(pool: asyncpg.Pool) -> None:
    async with asyncio.timeout(2):
        while _connections_held(pool):
            await asyncio.sleep(0.01)


async def test_vector_check_never_holds_a_request_pool_connection(monkeypatch, tmp_path):
    # pgvector on the main database, as deployed by default. The request
    # pool's only connection goes silent: a check that borrowed it would be
    # stuck returning it, and the pool would read as wedged. The check runs on
    # the probe's own connection instead, so the pool stays untouched.
    relay = _Relay()
    await relay.start()
    try:
        async with _ready_database(monkeypatch, tmp_path, pool_size=1, relay=relay) as db:
            main = db.main
            monkeypatch.setattr(main.settings, "readyz_pool_saturation_grace_secs", _GRACE)
            monkeypatch.setattr(main, "_READY_VECTOR_TIMEOUT_SECONDS", _GRACE)
            relay.freeze()
            responses = []
            for pause in (0.0, _PAST_GRACE):
                await asyncio.sleep(pause)
                main._ready_state.ts -= 31  # past the success cache
                responses.append(await db.client.get("/readyz"))
                assert db.pool.get_idle_size() == 1
            main._ready_state.ts -= 31
            responses.append(await db.client.get("/readyz"))
    finally:
        await relay.close()

    assert [response.status_code for response in responses] == [200] * 3, responses[-1].text
    assert {response.json()["detail"]["vector_store"] for response in responses} == {"ok"}
    assert db.store.calls == 3


async def test_vector_check_stuck_on_its_own_silent_connection_cannot_hold_readyz(monkeypatch, tmp_path):
    # pgvector with a database of its own borrows from its own pool. That
    # connection goes silent mid-query: the check is given up within its
    # budget, eleven callers return within the bound, and the connection is
    # terminated rather than left waiting to be returned.
    relay = _Relay()
    await relay.start()
    own_pool = None
    try:
        async with _ready_database(monkeypatch, tmp_path) as db:
            main = db.main
            own_dsn = _settings(db.name, address=relay.address).asyncpg_dsn
            store = _pgvector_store(dsn=own_dsn)
            # The test database has no vector extension for the store to set up.
            own_pool = await asyncpg.create_pool(dsn=own_dsn, min_size=1, max_size=1)
            store._own_pool = own_pool
            monkeypatch.setattr(main, "get_vector_store", lambda: store)
            monkeypatch.setattr(main, "_READY_VECTOR_TIMEOUT_SECONDS", _GRACE)
            relay.freeze()
            bound = 7.5  # inside kubelet's 8-second probe timeout

            started = time.monotonic()
            responses = await asyncio.wait_for(
                asyncio.gather(*(db.client.get("/readyz") for _ in range(11))), timeout=bound,
            )
            elapsed = time.monotonic() - started
            await _until_nothing_held(own_pool)
    finally:
        await relay.close()
        if own_pool is not None:
            own_pool.terminate()

    assert [response.status_code for response in responses] == [200] * 11
    assert {response.json()["detail"]["vector_store"] for response in responses} == {"degraded:timeout"}
    assert elapsed < _GRACE + 1.0


async def test_vector_check_cut_off_while_returning_its_connection_leaves_nothing_held(monkeypatch, tmp_path):
    # The check's query answers, but returning the connection to the store's
    # own pool never finishes. The check is given up within its budget, and
    # the connection is terminated rather than left held by that return.
    never = asyncio.Event()

    async def reset_that_never_finishes(_conn) -> None:
        await never.wait()

    own_pool = None
    try:
        async with _ready_database(monkeypatch, tmp_path) as db:
            main = db.main
            own_dsn = _settings(db.name).asyncpg_dsn
            store = _pgvector_store(dsn=own_dsn)
            own_pool = await asyncpg.create_pool(
                dsn=own_dsn, min_size=1, max_size=1, reset=reset_that_never_finishes,
            )
            store._own_pool = own_pool
            monkeypatch.setattr(main, "get_vector_store", lambda: store)
            monkeypatch.setattr(main, "_READY_VECTOR_TIMEOUT_SECONDS", _GRACE)

            response = await db.client.get("/readyz")
            await _until_nothing_held(own_pool)
    finally:
        never.set()
        if own_pool is not None:
            own_pool.terminate()

    assert response.status_code == 200, response.text
    assert response.json()["detail"]["vector_store"] == "degraded:timeout"


async def test_request_acquire_completing_before_the_probe_waits_is_not_lost(ready_db, monkeypatch):
    # The pool's only holder is still opening a request's connection when the
    # probe decides to check the pool. That connection becomes available the
    # moment the probe decides, so the request's acquire completes before the
    # probe has queued for a connection or registered to wait for progress.
    main = ready_db.main
    monkeypatch.setattr(main.settings, "readyz_pool_saturation_grace_secs", _GRACE)
    assert (await ready_db.client.get("/readyz")).status_code == 200  # opens the probe connection
    gated = _GatedConnect()
    pool = await _swap_in_a_gated_pool(ready_db, monkeypatch, gated, max_size=1)
    hold = asyncio.Event()
    try:
        await asyncio.sleep(_PAST_GRACE)

        async def serve_a_request() -> None:
            async with pool.acquire():
                await hold.wait()

        request = asyncio.create_task(serve_a_request())
        await gated.until_parked(1)
        real_unproven = main._pool_unproven

        def unproven_as_the_request_connects(checked_pool):
            unproven = real_unproven(checked_pool)
            gated.release()
            return unproven

        monkeypatch.setattr(main, "_pool_unproven", unproven_as_the_request_connects)
        main._ready_state.ts -= 31  # past the success cache
        started = time.monotonic()
        response = await ready_db.client.get("/readyz")
        elapsed = time.monotonic() - started
        hold.set()
        await request
    finally:
        hold.set()
        gated.release()
        pool.terminate()

    assert response.status_code == 200, response.text
    assert response.json()["detail"]["db"] == "ok"
    assert elapsed < main._READY_DB_TIMEOUT_SECONDS


async def test_request_taking_the_idle_connection_first_proves_the_pool_at_max_connections(monkeypatch, tmp_path):
    # The server refuses the probe's connection, and readiness turns to the
    # pool's idle connection. A request waiting to run takes it first and
    # keeps it; its completed acquire proves the pool.
    async with _ready_database(monkeypatch, tmp_path, role_connection_limit=1, pool_size=1) as db:
        main = db.main
        wake_the_request = asyncio.Event()
        hold = asyncio.Event()
        real_ping = main.readiness_ping

        async def refused_then_the_request_runs(**kwargs):
            try:
                await real_ping(**kwargs)
            except asyncpg.TooManyConnectionsError:
                wake_the_request.set()
                raise

        async def serve_a_request() -> None:
            await wake_the_request.wait()
            async with db.pool.acquire():
                await hold.wait()

        monkeypatch.setattr(main, "readiness_ping", refused_then_the_request_runs)
        request = asyncio.create_task(serve_a_request())
        try:
            started = time.monotonic()
            response = await db.client.get("/readyz")
            elapsed = time.monotonic() - started
        finally:
            hold.set()
            wake_the_request.set()
            await request

    assert response.status_code == 200, response.text
    assert response.json()["detail"]["db"] == "ok:pool_progress"
    assert elapsed < main._READY_DB_TIMEOUT_SECONDS


async def test_request_progress_wins_over_the_refusal_of_the_probes_extra_connection(monkeypatch, tmp_path):
    # The request's connection opens and the server refuses the probe's extra
    # one; both outcomes reach the pool together.
    async with _ready_database(monkeypatch, tmp_path, role_connection_limit=_POOL_SIZE + 1) as db:
        main = db.main
        monkeypatch.setattr(main.settings, "readyz_pool_saturation_grace_secs", _GRACE)
        assert (await db.client.get("/readyz")).status_code == 200  # opens the probe connection
        gated = _GatedConnect()
        pool = await _swap_in_a_gated_pool(db, monkeypatch, gated, max_size=2)
        hold = asyncio.Event()
        try:
            # Room for the probe's connection and one request's, no more.
            await db.admin.execute(f'ALTER ROLE "{db.name}" CONNECTION LIMIT 2')
            await asyncio.sleep(_PAST_GRACE)

            async def serve_a_request() -> None:
                async with pool.acquire():
                    await hold.wait()

            request = asyncio.create_task(serve_a_request())
            await gated.until_parked(1)

            async def release_both_outcomes() -> None:
                await gated.until_parked(2)  # the probe's extra connection was refused
                gated.release()

            releaser = asyncio.create_task(release_both_outcomes())
            main._ready_state.ts -= 31  # past the success cache
            response = await db.client.get("/readyz")
            await releaser
            hold.set()
            await request
        finally:
            hold.set()
            gated.release()
            pool.terminate()

    assert response.status_code == 200, response.text
    assert response.json()["detail"]["db"] == "ok"


async def test_pool_that_can_no_longer_open_connections_is_not_ready(monkeypatch, tmp_path):
    # The probe's own connection is opened first. Then the pool's connections
    # end, and the server lets the role keep only that one: requests could not
    # get a connection, while the probe's connection still answers.
    async with _ready_database(monkeypatch, tmp_path, role_connection_limit=_POOL_SIZE + 1) as db:
        monkeypatch.setattr(db.main.settings, "readyz_pool_saturation_grace_secs", _GRACE)
        assert (await db.client.get("/readyz")).status_code == 200
        assert await _probe_backend_pid(db) is not None

        pool_pids = await db.admin.fetch(
            "SELECT pid FROM pg_stat_activity WHERE datname = $1 AND application_name <> $2",
            db.name,
            postgres.READINESS_APPLICATION_NAME,
        )
        for row in pool_pids:
            await db.admin.fetchval("SELECT pg_terminate_backend($1)", row["pid"])
        async with asyncio.timeout(2):
            while db.pool.get_size() > 0:
                await asyncio.sleep(0.01)
        await db.admin.execute(f'ALTER ROLE "{db.name}" CONNECTION LIMIT 1')
        await asyncio.sleep(_PAST_GRACE)  # no acquire within the grace
        db.main._ready_state.ts -= 31  # past the success cache

        started = time.monotonic()
        response = await db.client.get("/readyz")
        elapsed = time.monotonic() - started

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]["detail"]
    assert detail["db"] == "error:pool_unavailable"
    assert detail["pool"]["size"] == 0 and detail["pool"]["idle"] == 0
    assert elapsed < db.main._READY_DB_TIMEOUT_SECONDS + 0.5


async def test_vector_query_cut_off_on_the_probe_connection_leaves_the_next_ping_a_fresh_one(monkeypatch, tmp_path):
    # The probe's own connection answers its ping, then goes silent: the
    # vector check on it times out and the cancel request asyncpg sends for it
    # never arrives. That connection must be given up, or the next probe's
    # ping would wait behind the unanswered cancel and fail readiness while
    # the database and the request pool are fine.
    relay = _Relay()
    await relay.start()
    try:
        async with _ready_database(monkeypatch, tmp_path, probe_relay=relay) as db:
            main = db.main
            observe = main._observe_pool

            def observe_then_silence_the_probe_connection(pool, state):
                observed = observe(pool, state)
                relay.freeze()
                return observed

            monkeypatch.setattr(main, "_observe_pool", observe_then_silence_the_probe_connection)
            first = await db.client.get("/readyz")
            monkeypatch.setattr(main, "_observe_pool", observe)
            relay.accept_new()
            main._ready_state.ts -= 31  # past the success cache
            second = await db.client.get("/readyz")
    finally:
        await relay.close()

    assert second.status_code == 200, second.text
    assert second.json()["detail"]["db"] == "ok"
    assert first.status_code == 200, first.text
    assert first.json()["detail"]["vector_store"] == "degraded:timeout"


async def test_database_gone_is_not_ready_at_once(ready_db):
    assert (await ready_db.client.get("/readyz")).status_code == 200
    assert await _probe_backend_pid(ready_db) is not None

    await ready_db.admin.execute(f'DROP DATABASE "{ready_db.name}" WITH (FORCE)')
    ready_db.main._ready_state.ts -= 31  # past the success cache

    started = time.monotonic()
    response = await ready_db.client.get("/readyz")
    elapsed = time.monotonic() - started

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["detail"]["db"] == "error:unavailable"
    assert elapsed < 2.0
    assert ready_db.name not in response.text


async def test_terminated_probe_connection_is_reopened_by_the_next_probe(ready_db):
    main = ready_db.main
    assert (await main._probe_ready())[0] is True
    first_pid = await _probe_backend_pid(ready_db)
    assert first_pid is not None
    conn = postgres._readiness_conn

    # As a server restart or an administrator would end it.
    assert await ready_db.admin.fetchval("SELECT pg_terminate_backend($1)", first_pid)
    async with asyncio.timeout(2):
        while not conn.is_closed():
            await asyncio.sleep(0.01)

    ok, detail = await main._probe_ready()
    assert ok is True, detail
    second_pid = await _probe_backend_pid(ready_db)
    assert second_pid is not None and second_pid != first_pid


async def test_probe_connection_is_separate_from_the_pool(ready_db):
    assert (await ready_db.main._probe_ready())[0] is True

    names = await ready_db.admin.fetch(
        "SELECT application_name FROM pg_stat_activity WHERE datname = $1",
        ready_db.name,
    )
    probe_sessions = [row for row in names if row["application_name"] == postgres.READINESS_APPLICATION_NAME]
    assert len(probe_sessions) == 1
    # The pool keeps its own connections; the probe borrowed none of them.
    assert ready_db.pool.get_idle_size() == _POOL_SIZE


async def test_shutdown_closes_the_probe_connection(ready_db, monkeypatch):
    from app.services import lifecycle

    assert (await ready_db.main._probe_ready())[0] is True
    pid = await _probe_backend_pid(ready_db)
    conn = postgres._readiness_conn
    assert pid is not None and conn is not None

    order: list[str] = []
    real_close_probe, real_close_pool = lifecycle.close_readiness_connection, lifecycle.close_pool

    async def close_probe() -> None:
        order.append("readiness_connection")
        await real_close_probe()

    async def close_pool() -> None:
        order.append("pool")
        await real_close_pool()

    monkeypatch.setattr(lifecycle.settings, "keycloak_enabled", False)
    # Another test's HTTP client, if any, is not this test's to close.
    monkeypatch.setattr(lifecycle.http_pool, "close_client", lambda: asyncio.sleep(0))
    monkeypatch.setattr(lifecycle, "close_readiness_connection", close_probe)
    monkeypatch.setattr(lifecycle, "close_pool", close_pool)

    await lifecycle.shutdown_storage()

    assert order == ["readiness_connection", "pool"]
    assert conn.is_closed()
    assert postgres._readiness_conn is None
    async with asyncio.timeout(2):
        while await _probe_backend_pid(ready_db) is not None:
            await asyncio.sleep(0.01)
