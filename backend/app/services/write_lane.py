"""Write-lane admission control for git-committing write paths.

Design: docs/design/proposal/command-lane-write-path (round-05).

Two independent starvation paths existed on the write path, both caused by
the same mistake — WAITING while holding a scarce resource:

1. Pool poisoning: a writer queued for the per-vault git lock while holding
   a pool connection inside an open transaction. Enough same-vault writers
   drained the global pool and stalled every read on every vault.
2. Executor starvation: that queueing happened INSIDE a thread of the shared
   asyncio.to_thread executor (``min(32, cpu+4)`` — 12 on an 8-core node).
   Blocked waiters ate the pool that document reads (also to_thread) need,
   so a hot vault froze git reads service-wide before the PG pool even ran dry.

The fix moves admission in front of resource acquisition, into event-loop
space where waiting is a suspended coroutine (a few KB of memory, no thread,
no connection):

    request → per-vault gate (1 concurrent; FIFO)        ← coroutine wait
            → global lane semaphore (write_lane_concurrency)
            → only now: pool connection + advisory lock + transaction
            → git commit via the DEDICATED commit executor
            → PG writes → transaction commit → release everything

Order matters: the per-vault gate comes FIRST. If the global slots were
taken first, a hot vault's waiters would camp on them while queueing for
their own vault gate — re-creating cross-vault contagion at the lane level.

The per-vault ``threading.Lock`` in git_service stays as the last-line
correctness guard (worker paths — external_git poller, vault seeding —
commit without passing this lane); with API traffic gated here it is
uncontended in the steady state.

Both gate stages share one deadline (``write_lane_queue_timeout_secs``);
exceeding it raises :class:`~app.exceptions.WriteBusyError` → HTTP 429 +
Retry-After / MCP ``write_busy`` envelope. No work has been performed at
that point, so retrying is always safe. ``write_lane_max_waiters`` is a
global memory/socket backstop, not a policy knob — normal operation never
reaches it.

Like git_service's ``_VAULT_LOCKS``, gate entries are keyed by vault name
and never reaped; a deleted vault leaves a few idle bytes behind, bounded
by the total number of vaults ever written to in this process.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Callable, TypeVar

from app.config import settings
from app.exceptions import WriteBusyError

logger = logging.getLogger("akb.write_lane")

T = TypeVar("T")

# True while the current task holds a global lane slot (set inside
# write_lane). run_git_write consults it so ungated callers (vault
# lifecycle: init/seed/template/cleanup) transparently acquire a slot of
# their own before touching the commit executor — the executor therefore
# only ever receives slot holders and can never build a queue that
# admitted writers (holding PG connections) would have to sit in.
_slot_held: ContextVar[bool] = ContextVar("akb_write_slot_held", default=False)

# Per-vault admission gates (1 concurrent writer per vault — git commits
# serialize per vault anyway, so more buys nothing while git is on the
# synchronous path; see round-05 "granularity").
_vault_gates: dict[str, asyncio.Lock] = {}

# Global write-lane semaphore — caps writes as a CLASS, so the connections
# and commit threads they can occupy are bounded no matter how many vaults
# are active at once.
_global_slots: asyncio.Semaphore | None = None

# Writers currently inside the admission wait (both stages). Backstop only.
_waiters: int = 0

# Ungated (vault-lifecycle) calls currently waiting for a global slot in
# run_git_write. Observability only — no backstop: these are rare
# operator-grade operations and must not fail with 429 at saturation.
_lifecycle_waiters: int = 0

# Dedicated executor for git-MUTATING operations. Git reads keep using
# asyncio.to_thread's default executor; commits physically cannot crowd
# them out. Sized to write_lane_concurrency — admitted writers only, so
# there is never queueing here in the steady state.
_commit_pool: ThreadPoolExecutor | None = None


def _vault_gate(vault: str) -> asyncio.Lock:
    gate = _vault_gates.get(vault)
    if gate is None:
        gate = asyncio.Lock()
        _vault_gates[vault] = gate
    return gate


def _global_sem() -> asyncio.Semaphore:
    global _global_slots
    if _global_slots is None:
        _global_slots = asyncio.Semaphore(max(1, settings.write_lane_concurrency))
    return _global_slots


@asynccontextmanager
async def write_lane(vault: str):
    """Admit one git-committing write for ``vault``.

    Enter BEFORE acquiring any pool connection / advisory lock; hold for
    the whole critical section (git commit + PG transaction). Raises
    :class:`WriteBusyError` (→ 429) when the deadline or the global waiter
    backstop is exceeded — in both cases no work has been done yet.
    """
    global _waiters
    if _waiters >= settings.write_lane_max_waiters:
        logger.warning(
            "write lane backstop hit (%d waiters) — rejecting write for vault %s",
            _waiters, vault,
        )
        raise WriteBusyError(vault, 0.0)

    gate = _vault_gate(vault)
    sem = _global_sem()
    timeout = settings.write_lane_queue_timeout_secs
    got_gate = got_slot = False
    slot_token = None
    _waiters += 1
    try:
        try:
            async with asyncio.timeout(timeout):
                await gate.acquire()
                got_gate = True
                await sem.acquire()
                got_slot = True
        except TimeoutError:
            logger.warning(
                "write admission timed out after %.0fs for vault %s (%d waiters)",
                timeout, vault, _waiters,
            )
            raise WriteBusyError(vault, timeout) from None
        finally:
            _waiters -= 1
        slot_token = _slot_held.set(True)
        yield
    finally:
        if slot_token is not None:
            _slot_held.reset(slot_token)
        if got_slot:
            sem.release()
        if got_gate:
            gate.release()


def start_commit_pool() -> None:
    """Create the dedicated git-commit executor. Idempotent."""
    global _commit_pool
    if _commit_pool is not None:
        return
    n = max(1, settings.write_lane_concurrency)
    _commit_pool = ThreadPoolExecutor(
        max_workers=n, thread_name_prefix="akb-git-commit",
    )
    logger.info("git commit executor started (workers=%d)", n)


def stop_commit_pool() -> None:
    """Shut down the commit executor.

    ``wait=False``: by the time lifespan shutdown runs, uvicorn has drained
    in-flight requests, so the pool is idle; any straggler thread is joined
    at interpreter exit (ThreadPoolExecutor threads are non-daemon).
    """
    global _commit_pool
    if _commit_pool is None:
        return
    _commit_pool.shutdown(wait=False)
    _commit_pool = None
    logger.info("git commit executor stopped")


async def run_git_write(
    fn: Callable[..., T], /, *args: Any, must_complete: bool = False, **kwargs: Any,
) -> T:
    """Run a git-MUTATING call on the dedicated commit executor.

    Use for commit_file / delete_file / move_file / delete_paths_bulk and
    the vault-lifecycle mutations (init/seed/template/cleanup). Git READS
    stay on asyncio.to_thread — the whole point is that these two classes
    of work never share a thread pool.

    Slot discipline: callers inside ``write_lane`` already hold a global
    lane slot (tracked via ContextVar); everyone else transparently
    acquires one here first, as a plain coroutine wait with no deadline —
    vault-lifecycle operations are rare and must not 429, but they must
    also never occupy the executor beyond the lane's global budget.
    Invariant: the commit executor only ever runs slot holders, so its
    internal queue stays empty and an admitted writer (holding a PG
    connection) never waits behind ungated work.

    Cancellation: an executor thread cannot be interrupted. If the caller
    is cancelled mid-commit we absorb the cancellation until the thread
    finishes (bounded by ``git_write_timeout_secs`` on the git side) and
    only then propagate it. Unwinding immediately would release the lane
    and the caller's resources while the commit still holds the per-vault
    threading.Lock — the next admitted writer would then block on that
    lock inside the executor holding a PG connection, which is the exact
    pathology the lane exists to prevent.

    ``must_complete=True`` extends that absorption to the slot WAIT as
    well: cancellation while queueing does not abort the call — the work
    still runs to completion and only then is the cancellation delivered.
    For compensation writes that must not be lost once their caller has
    passed a point of no return (delete_vault's on-disk cleanup after the
    DB cascade committed; create_vault's rollback cleanup). Without it, a
    client disconnect in the queueing gap strands state the request can
    never repair (e.g. orphan bare dirs that block a same-name recreate).
    """
    if _slot_held.get():
        return await _dispatch_to_pool(fn, *args, **kwargs)

    global _lifecycle_waiters
    sem = _global_sem()
    pending_cancel = False
    _lifecycle_waiters += 1
    try:
        while True:
            try:
                await sem.acquire()
                break
            except asyncio.CancelledError:
                if not must_complete:
                    raise
                pending_cancel = True
    finally:
        _lifecycle_waiters -= 1
    try:
        result = await _dispatch_to_pool(fn, *args, **kwargs)
    finally:
        sem.release()
    if pending_cancel:
        # The work is done; now deliver the cancellation we absorbed
        # during the slot wait.
        raise asyncio.CancelledError()
    return result


async def _dispatch_to_pool(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    if _commit_pool is None:
        # Lazy fallback for tests and callers outside the app lifespan.
        start_commit_pool()
    loop = asyncio.get_running_loop()
    assert _commit_pool is not None
    fut = loop.run_in_executor(_commit_pool, functools.partial(fn, *args, **kwargs))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        if fut.cancelled():
            # Never started (still queued when cancelled) — nothing runs,
            # nothing to wait for.
            raise
        # Thread is (or may be) running: hold our lane/slot until it
        # finishes, absorbing any further cancellation attempts.
        while not fut.done():
            try:
                await asyncio.wait([fut])
            except asyncio.CancelledError:
                continue
        # The fn's outcome is discarded in favor of the cancellation —
        # log it so an absorbed failure (e.g. FileExistsError from
        # init_vault on an existing name) stays diagnosable.
        discarded = fut.exception()
        logger.info(
            "git write %s finished after caller cancellation; %s discarded "
            "(a stray commit may exist until the next write reconciles the worktree)",
            getattr(fn, "__name__", str(fn)),
            f"exception {discarded!r}" if discarded else "result",
        )
        raise


async def run_compensation(coro) -> Any:
    """Run a compensation coroutine to COMPLETION, absorbing cancellation
    until it finishes, then re-delivering the cancellation.

    ``asyncio.shield`` alone is not enough for compensation steps: on
    cancellation it raises immediately and lets the inner work continue as
    a DETACHED task — during shutdown the pool can close underneath it,
    leaving the compensation half-done with nobody watching. This helper
    keeps the caller parked (as a coroutine, holding whatever locks the
    compensation relies on) until the inner task actually completes.

    If the inner work raises while a cancellation was absorbed, the
    cancellation wins and the work's exception is logged (mirrors
    ``_dispatch_to_pool``'s discard rule).
    """
    task = asyncio.ensure_future(coro)
    absorbed: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as ce:
            if task.cancelled():
                raise  # the inner task itself was cancelled — nothing to wait for
            absorbed = ce  # outer cancel; keep waiting for the inner task
        except BaseException:  # noqa: BLE001 — task's own error; task is done, loop exits
            break
    if absorbed is not None:
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.error(
                "compensation step failed while absorbing a cancellation: %r "
                "(cancellation takes precedence; state may need operator review)",
                exc,
            )
        raise absorbed
    return task.result()


def snapshot() -> dict:
    """Cheap observability hook (health/debug): current admission state."""
    return {
        "waiters": _waiters,
        "lifecycle_waiters": _lifecycle_waiters,
        "capacity": max(1, settings.write_lane_concurrency),
        "vault_gates": len(_vault_gates),
        "commit_pool_started": _commit_pool is not None,
    }


def _reset_for_tests() -> None:
    """Drop all lane state. Tests only — never call in production code.

    asyncio primitives bind to the loop they first wait on; pytest creates
    a fresh loop per test, so module-level gates must be discarded between
    tests or cross-loop reuse raises RuntimeError.
    """
    global _global_slots, _waiters, _lifecycle_waiters
    stop_commit_pool()
    _vault_gates.clear()
    _global_slots = None
    _waiters = 0
    _lifecycle_waiters = 0
