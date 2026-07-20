"""Regression: the /activity route must run git.vault_log OFF the event loop.

`git.vault_log` spawns `git rev-list` + one `git diff` subprocess per commit
(linear in the commit window); called inline on the single event loop it
starves the loop under concurrency → `/livez` probe timeout → 503 (2026-07-20
audit). The route must `await asyncio.to_thread(git.vault_log, ...)`. This test
blocks the git call and asserts a concurrent heartbeat keeps ticking, i.e. a
revert that inlines the call regresses.

No DB — the route's other deps are monkeypatched. Runs in `pytest -k 'not _e2e'`.
"""

import asyncio
import time

from app.api.routes import activity

_N = 12
_BLOCK = 0.15  # each fake git call blocks the calling thread this long


class _FakeUser:
    user_id = "00000000-0000-0000-0000-000000000000"
    username = "t"


async def _heartbeat(done: asyncio.Event, gaps: list[float]) -> None:
    last = time.perf_counter()
    while not done.is_set():
        await asyncio.sleep(0.01)
        now = time.perf_counter()
        gaps.append(now - last)
        last = now


async def test_vault_activity_offloads_git_vault_log(monkeypatch):
    async def _noop_access(*a, **k):
        return {"role": "reader"}

    async def _identity(entries):
        return entries

    def _blocking_vault_log(*a, **k):
        # Stand in for the rev-list + per-commit diff subprocess cost. Like a
        # subprocess (and unlike a pure-Python CPU loop) time.sleep RELEASES
        # the GIL, so an off-loop worker thread genuinely runs it in parallel.
        time.sleep(_BLOCK)
        return []

    monkeypatch.setattr(activity, "check_vault_access", _noop_access)
    monkeypatch.setattr(activity, "_resolve_activity_authors", _identity)
    monkeypatch.setattr(activity.git, "vault_log", _blocking_vault_log)

    done = asyncio.Event()
    gaps: list[float] = []
    hb = asyncio.create_task(_heartbeat(done, gaps))
    await asyncio.gather(
        *(
            activity.vault_activity(
                vault="v", collection=None, author=None, since=None,
                limit=100, user=_FakeUser(),
            )
            for _ in range(_N)
        )
    )
    done.set()
    await hb

    # Offloaded: the loop keeps servicing 10ms heartbeats (gaps ~10-30ms).
    # Inlined: the very first call blocks the loop for a full _BLOCK, so a gap
    # would be >= _BLOCK. 0.7*_BLOCK cleanly separates the two.
    assert gaps, "heartbeat never ran"
    assert max(gaps) < _BLOCK * 0.7, (
        f"event loop stalled {max(gaps):.3f}s under {_N} concurrent "
        "/activity calls — git.vault_log is running on the loop again"
    )
