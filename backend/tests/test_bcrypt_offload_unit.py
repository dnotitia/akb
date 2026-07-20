"""Regression: bcrypt must run OFF the single event loop.

2026-07-20 prod incident: concurrent auth (login/register/MCP-session)
ran bcrypt (cost 12, ~hundreds of ms, on the event loop) which starved the
single asyncio loop → `/livez` liveness probe timed out (`awaiting headers`)
→ kubelet SIGKILL (exit 137) → whole service 503 (backend is replicas=1).

`hash_password_async` / `verify_password_async` offload bcrypt to a worker
thread (bcrypt releases the GIL, so a thread genuinely runs it off-loop and
concurrent hashes overlap). These tests assert the loop stays responsive
while many hashes run concurrently — i.e. re-inlining bcrypt regresses.

No DB needed — pure event-loop behavior. Runs in `pytest -k 'not _e2e'`.
"""

import asyncio
import time

from app.services.auth_service import (
    hash_password,
    hash_password_async,
    verify_password_async,
)

# Enough concurrent bcrypts that, if run ON the loop, they serialize into a
# multi-second stall (N * ~0.2s). Offloaded, the loop barely notices.
_N = 16
# On-loop regression would push the heartbeat gap to ~3s+; offloaded it stays
# well under 100ms. 1.5s cleanly separates the two on slow CI too.
_MAX_LOOP_GAP_S = 1.5


async def _max_heartbeat_gap(work: asyncio.Future) -> float:
    """Tick every 10ms until ``work`` completes; return the worst gap between
    ticks. A blocked loop can't service the ticks, so the gap balloons."""
    max_gap = 0.0
    last = time.perf_counter()
    while not work.done():
        await asyncio.sleep(0.01)
        now = time.perf_counter()
        max_gap = max(max_gap, now - last)
        last = now
    return max_gap


async def test_verify_password_async_does_not_block_event_loop():
    pw_hash = hash_password("correct-horse-battery-staple")
    # Wrong password still runs the full bcrypt.checkpw — the hot path in the
    # incident (concurrent failed logins).
    work = asyncio.gather(
        *(verify_password_async("wrong-guess", pw_hash) for _ in range(_N))
    )
    max_gap = await _max_heartbeat_gap(work)
    results = await work
    assert results == [False] * _N
    assert max_gap < _MAX_LOOP_GAP_S, (
        f"event loop stalled {max_gap:.3f}s under {_N} concurrent verifies — "
        "bcrypt is running on the loop again"
    )


async def test_hash_password_async_roundtrips_without_blocking():
    pw = "s3cret-Passw0rd!"
    work = asyncio.gather(*(hash_password_async(pw) for _ in range(_N)))
    max_gap = await _max_heartbeat_gap(work)
    hashes = await work
    assert all(h.startswith("$2") for h in hashes)  # bcrypt hash marker
    verified = await asyncio.gather(*(verify_password_async(pw, h) for h in hashes))
    assert all(verified)
    assert max_gap < _MAX_LOOP_GAP_S, (
        f"event loop stalled {max_gap:.3f}s under {_N} concurrent hashes — "
        "bcrypt is running on the loop again"
    )
