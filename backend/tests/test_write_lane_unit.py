"""Unit tests for write-lane admission (command-lane round-05).

Covers the gate semantics in isolation — no DB, no git:

- per-vault serialization (1 concurrent writer per vault)
- cross-vault independence (a hot vault's backlog never parks another vault)
- global lane cap (writes as a class are bounded)
- deadline → WriteBusyError(429) with no leaked gate state
- waiter backstop
- cancellation while queued leaves no residue
- FIFO admission order
- invariants under a mixed-vault stress burst
- run_git_write executes on the dedicated commit executor
- REST (Retry-After header) and MCP (write_busy envelope) error surfaces
"""

import asyncio
import tempfile
import threading
from collections import defaultdict

import pytest

from app.config import settings
from app.exceptions import NotFoundError, WriteBusyError
from app.services import write_lane

# app.main / mcp_server.server instantiate DocumentService() at module load,
# which mkdirs the git storage path — point it somewhere writable so the
# lazy imports inside the error-surface tests don't blow up on `/data/vaults`
# (same pattern as test_mcp_oauth_unit).
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-write-lane-test-vaults-")


@pytest.fixture(autouse=True)
def _fresh_lane():
    # asyncio primitives bind to the first loop that awaits them; pytest
    # gives each test its own loop, so lane state must be dropped around
    # every test.
    write_lane._reset_for_tests()
    yield
    write_lane._reset_for_tests()


async def test_same_vault_serializes():
    order = []
    first_in = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with write_lane.write_lane("v"):
            order.append("first-in")
            first_in.set()
            await release.wait()
        order.append("first-out")

    async def second():
        await first_in.wait()
        async with write_lane.write_lane("v"):
            order.append("second-in")

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    await first_in.wait()
    await asyncio.sleep(0.05)  # give `second` a chance to (wrongly) enter
    assert order == ["first-in"], "second writer entered a held vault gate"
    release.set()
    await asyncio.gather(t1, t2)
    assert order == ["first-in", "first-out", "second-in"]


async def test_other_vault_not_blocked_by_hot_vault():
    release = asyncio.Event()
    hog_in = asyncio.Event()

    async def hog():
        async with write_lane.write_lane("hot"):
            hog_in.set()
            await release.wait()

    task = asyncio.create_task(hog())
    await hog_in.wait()
    # Must acquire promptly — the hot vault's occupancy is irrelevant.
    async with asyncio.timeout(1.0):
        async with write_lane.write_lane("cold"):
            pass
    release.set()
    await task


async def test_global_cap_bounds_concurrent_writers(monkeypatch):
    monkeypatch.setattr(settings, "write_lane_concurrency", 2)
    active = 0
    peak = 0
    release = asyncio.Event()

    async def writer(vault: str):
        nonlocal active, peak
        async with write_lane.write_lane(vault):
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1

    tasks = [asyncio.create_task(writer(f"v{i}")) for i in range(4)]
    await asyncio.sleep(0.05)
    assert active == 2, "global lane admitted more than write_lane_concurrency"
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


async def test_deadline_raises_write_busy_and_releases_state(monkeypatch):
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 0.05)
    release = asyncio.Event()
    hog_in = asyncio.Event()

    async def hog():
        async with write_lane.write_lane("v"):
            hog_in.set()
            await release.wait()

    task = asyncio.create_task(hog())
    await hog_in.wait()
    with pytest.raises(WriteBusyError) as ei:
        async with write_lane.write_lane("v"):
            pass
    assert ei.value.status_code == 429
    assert ei.value.retry_after_secs >= 1
    release.set()
    await task
    # The timed-out waiter must not leak the gate: reacquire promptly.
    async with asyncio.timeout(1.0):
        async with write_lane.write_lane("v"):
            pass
    assert write_lane.snapshot()["waiters"] == 0


async def test_global_slot_timeout_releases_vault_gate(monkeypatch):
    # Vault gate acquired, then the wait for the (exhausted) global slot
    # times out — the vault gate must be handed back.
    monkeypatch.setattr(settings, "write_lane_concurrency", 1)
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 0.05)
    release = asyncio.Event()
    hog_in = asyncio.Event()

    async def hog():
        async with write_lane.write_lane("a"):
            hog_in.set()
            await release.wait()

    task = asyncio.create_task(hog())
    await hog_in.wait()
    with pytest.raises(WriteBusyError):
        async with write_lane.write_lane("b"):
            pass
    release.set()
    await task
    async with asyncio.timeout(1.0):
        async with write_lane.write_lane("b"):
            pass


async def test_waiter_backstop_rejects_immediately(monkeypatch):
    monkeypatch.setattr(settings, "write_lane_max_waiters", 0)
    with pytest.raises(WriteBusyError):
        async with write_lane.write_lane("v"):
            pass


async def test_exception_inside_lane_releases_gate():
    with pytest.raises(ValueError):
        async with write_lane.write_lane("v"):
            raise ValueError("boom")
    async with asyncio.timeout(1.0):
        async with write_lane.write_lane("v"):
            pass


async def test_cancellation_while_waiting_releases_state():
    release = asyncio.Event()
    hog_in = asyncio.Event()

    async def hog():
        async with write_lane.write_lane("v"):
            hog_in.set()
            await release.wait()

    t = asyncio.create_task(hog())
    await hog_in.wait()

    async def waiter():
        async with write_lane.write_lane("v"):
            pass

    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)  # let the waiter actually park on the gate
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert write_lane.snapshot()["waiters"] == 0
    release.set()
    await t
    # Cancelled waiter must not have corrupted the gate.
    async with asyncio.timeout(1.0):
        async with write_lane.write_lane("v"):
            pass


async def test_fifo_admission_order():
    order: list[int] = []
    release = asyncio.Event()
    hog_in = asyncio.Event()

    async def hog():
        async with write_lane.write_lane("v"):
            hog_in.set()
            await release.wait()

    t = asyncio.create_task(hog())
    await hog_in.wait()

    async def writer(i: int):
        async with write_lane.write_lane("v"):
            order.append(i)

    tasks = []
    for i in range(5):
        tasks.append(asyncio.create_task(writer(i)))
        await asyncio.sleep(0.01)  # deterministic arrival order
    release.set()
    await asyncio.gather(t, *tasks)
    assert order == [0, 1, 2, 3, 4], "vault gate is not FIFO — starvation possible"


async def test_stress_invariants_hold_and_state_drains(monkeypatch):
    """150 writers over 7 vaults: per-vault ≤ 1, global ≤ M, and after the
    burst every gate is reacquirable with zero waiters — no leaked state."""
    monkeypatch.setattr(settings, "write_lane_concurrency", 4)
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 5.0)

    per_vault_active: dict[str, int] = defaultdict(int)
    global_active = 0
    violations: list[tuple] = []

    async def writer(vault: str):
        nonlocal global_active
        async with write_lane.write_lane(vault):
            per_vault_active[vault] += 1
            global_active += 1
            if per_vault_active[vault] > 1:
                violations.append(("per-vault", vault, per_vault_active[vault]))
            if global_active > 4:
                violations.append(("global", global_active))
            await asyncio.sleep(0.001)
            per_vault_active[vault] -= 1
            global_active -= 1

    tasks = [asyncio.create_task(writer(f"v{i % 7}")) for i in range(150)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    hard_errors = [
        r for r in results
        if isinstance(r, BaseException) and not isinstance(r, WriteBusyError)
    ]
    assert not violations, f"concurrency invariant violated: {violations[:5]}"
    assert not hard_errors, f"unexpected errors: {hard_errors[:5]}"
    assert write_lane.snapshot()["waiters"] == 0
    async with asyncio.timeout(1.0):
        for i in range(7):
            async with write_lane.write_lane(f"v{i}"):
                pass


async def test_cancel_during_commit_absorbs_until_thread_done(monkeypatch):
    """Cancelling a writer mid-commit must NOT release the lane while the
    executor thread still runs (the thread holds the vault threading.Lock;
    releasing early would let the next writer block on it holding a PG
    connection — codex round-1 finding 1)."""
    started = threading.Event()
    release = threading.Event()

    def slow_commit():
        started.set()
        release.wait(5)
        return "sha"

    async def writer():
        async with write_lane.write_lane("v"):
            await write_lane.run_git_write(slow_commit)

    t = asyncio.create_task(writer())
    await asyncio.to_thread(started.wait, 5)
    t.cancel()
    await asyncio.sleep(0.05)
    # The writer must be absorbing the cancellation, not unwound.
    assert not t.done(), "writer unwound while its git thread was still running"
    # And the vault gate must still be held on its behalf.
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 0.05)
    with pytest.raises(WriteBusyError):
        async with write_lane.write_lane("v"):
            pass
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await t
    # Once the thread finished, cancellation propagated and the lane drained.
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 10.0)
    async with asyncio.timeout(1.0):
        async with write_lane.write_lane("v"):
            pass
    assert write_lane.snapshot()["waiters"] == 0


async def test_lifecycle_write_consumes_global_slot(monkeypatch):
    """Ungated run_git_write callers (vault lifecycle) must acquire a global
    lane slot before touching the executor, so the executor never queues
    work that admitted writers would wait behind (codex round-1 finding 2)."""
    monkeypatch.setattr(settings, "write_lane_concurrency", 1)
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 0.1)
    started = threading.Event()
    release = threading.Event()

    def lifecycle_op():
        started.set()
        release.wait(5)

    task = asyncio.create_task(write_lane.run_git_write(lifecycle_op))
    await asyncio.to_thread(started.wait, 5)
    # The single global slot is occupied by the lifecycle op → admission
    # times out instead of queueing in the executor.
    with pytest.raises(WriteBusyError):
        async with write_lane.write_lane("v"):
            pass
    release.set()
    await task
    async with asyncio.timeout(1.0):
        async with write_lane.write_lane("v"):
            pass
    snap = write_lane.snapshot()
    assert snap["waiters"] == 0 and snap["lifecycle_waiters"] == 0


async def test_must_complete_survives_cancel_during_slot_wait(monkeypatch):
    """A must_complete lifecycle write (delete_vault cleanup, create_vault
    rollback) cancelled while QUEUEING for a slot must still run to
    completion, then deliver the cancellation (codex round-2 finding 2)."""
    monkeypatch.setattr(settings, "write_lane_concurrency", 1)
    ran = threading.Event()
    hog_release = threading.Event()
    hog_started = threading.Event()

    def hog_op():
        hog_started.set()
        hog_release.wait(5)

    def compensation_op():
        ran.set()

    hog = asyncio.create_task(write_lane.run_git_write(hog_op))
    await asyncio.to_thread(hog_started.wait, 5)

    comp = asyncio.create_task(
        write_lane.run_git_write(compensation_op, must_complete=True)
    )
    await asyncio.sleep(0.05)  # comp is now parked on the slot wait
    comp.cancel()
    await asyncio.sleep(0.05)
    assert not comp.done(), "must_complete task aborted during slot wait"
    assert not ran.is_set()
    hog_release.set()
    await hog
    with pytest.raises(asyncio.CancelledError):
        await comp
    assert ran.is_set(), "compensation work was lost despite must_complete"
    assert write_lane.snapshot()["lifecycle_waiters"] == 0


async def test_default_lifecycle_cancel_during_slot_wait_aborts(monkeypatch):
    """Without must_complete, cancellation while queueing aborts cleanly —
    the work never runs and no slot state leaks."""
    monkeypatch.setattr(settings, "write_lane_concurrency", 1)
    ran = threading.Event()
    hog_release = threading.Event()
    hog_started = threading.Event()

    def hog_op():
        hog_started.set()
        hog_release.wait(5)

    hog = asyncio.create_task(write_lane.run_git_write(hog_op))
    await asyncio.to_thread(hog_started.wait, 5)

    task = asyncio.create_task(write_lane.run_git_write(ran.set))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    hog_release.set()
    await hog
    assert not ran.is_set()
    assert write_lane.snapshot()["lifecycle_waiters"] == 0
    # Slot fully returned: a fresh acquisition succeeds promptly.
    async with asyncio.timeout(1.0):
        async with write_lane.write_lane("v"):
            pass


async def test_run_compensation_absorbs_cancel_until_done():
    """run_compensation must keep the caller parked until the inner work
    completes even when cancelled — a bare shield detaches the work and
    lets shutdown close resources underneath it (codex round-4 finding 1)."""
    release = asyncio.Event()
    completed = asyncio.Event()

    async def compensation():
        await release.wait()
        completed.set()
        return "done"

    async def caller():
        await write_lane.run_compensation(compensation())

    t = asyncio.create_task(caller())
    await asyncio.sleep(0.02)
    t.cancel()
    await asyncio.sleep(0.02)
    assert not t.done(), "caller unwound while the compensation was still running"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert completed.is_set(), "compensation was abandoned mid-flight"


async def test_run_compensation_returns_result_without_cancel():
    async def compensation():
        return 42

    assert await write_lane.run_compensation(compensation()) == 42


async def test_run_compensation_propagates_inner_error():
    async def compensation():
        raise RuntimeError("purge failed")

    with pytest.raises(RuntimeError, match="purge failed"):
        await write_lane.run_compensation(compensation())


async def test_lane_holder_dispatch_does_not_double_acquire(monkeypatch):
    """A writer already holding a lane slot must dispatch directly — if
    run_git_write re-acquired the semaphore, M=1 would deadlock here."""
    monkeypatch.setattr(settings, "write_lane_concurrency", 1)
    async with asyncio.timeout(2.0):
        async with write_lane.write_lane("v"):
            out = await write_lane.run_git_write(lambda: 7)
    assert out == 7


async def test_run_git_write_uses_dedicated_pool():
    def probe(x: int):
        return threading.current_thread().name, x * 2

    name, val = await write_lane.run_git_write(probe, 21)
    assert val == 42
    assert name.startswith("akb-git-commit")


async def test_run_git_write_propagates_exception():
    def boom():
        raise RuntimeError("kaput")

    with pytest.raises(RuntimeError, match="kaput"):
        await write_lane.run_git_write(boom)


def test_write_busy_error_envelope():
    from app.util.errors import WRITE_BUSY, exception_envelope

    env = exception_envelope(WriteBusyError("v", 10.0))
    assert env["code"] == WRITE_BUSY
    assert env["details"]["retry_after_secs"] == 5
    assert env["hint"]


async def test_rest_handler_maps_429_with_retry_after():
    from app.main import akb_error_handler

    resp = await akb_error_handler(None, WriteBusyError("v", 10.0))
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "5"

    # Non-write-busy AKBErrors must not grow a Retry-After header.
    resp2 = await akb_error_handler(None, NotFoundError("Vault", "x"))
    assert resp2.status_code == 404
    assert "Retry-After" not in resp2.headers


async def test_mcp_dispatch_maps_write_busy(monkeypatch):
    from mcp_server import server as srv
    from app.util.errors import WRITE_BUSY

    async def busy_handler(args, uid, user):
        raise WriteBusyError("v", 10.0)

    monkeypatch.setitem(srv._HANDLERS, "akb_fake_busy_tool", busy_handler)
    out = await srv._dispatch("akb_fake_busy_tool", {}, srv._MCPUser())
    assert out["code"] == WRITE_BUSY
    assert out["details"]["retry_after_secs"] == 5
    assert out["hint"]
