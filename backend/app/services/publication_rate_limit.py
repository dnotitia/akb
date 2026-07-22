"""In-memory throttle for public-publication password attempts (publish-hardening F2).

The backend runs single-replica (one process, one event loop), so a module-global
dict is authoritative without cross-process coordination. It resets on restart —
acceptable, because bcrypt (~250 ms/try, offloaded) *plus* this lockout make
online brute force of a share password infeasible, and a restart is far rarer than
a lockout window. Redis is optional here (only the event stream uses it), so we
deliberately do NOT depend on it.

Design notes (both from the F2 Codex review):
  - Attempts are counted in `reserve()` BEFORE the (slow, awaited) bcrypt verify,
    so a concurrent burst can't all slip past a stale counter — the Nth in-flight
    attempt already sees the incremented count.
  - Counters DECAY: a quiet period (`_DECAY_SECS`) resets a key, so occasional
    typos over days can't accumulate into a permanent escalating lockout.
  - Two keys: per-(slug, ip) tight, plus a per-slug backstop an attacker cannot
    dodge by rotating the (spoofable) X-Forwarded-For. `release()` undoes the
    speculative count for a verified-correct attempt so legitimate views never
    drift toward the backstop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

_FREE_IP = 5             # wrong attempts per (slug, ip) before backoff
_FREE_SLUG = 30          # wrong attempts per slug (all IPs) before backoff
_BACKOFF = (10.0, 30.0, 120.0, 600.0, 1800.0, 3600.0)  # seconds per step past free
_DECAY_SECS = 3600.0     # a quiet hour resets a key (no permanent lockout from stray typos)
_MAX_ENTRIES = 20_000    # per-map cap; evict least-recently-used half when exceeded


@dataclass
class _E:
    failures: int = 0
    locked_until: float = 0.0
    last: float = 0.0


_by_ip: dict[tuple[str, str], _E] = {}
_by_slug: dict[str, _E] = {}


def _wait(e: _E | None, now: float) -> float:
    return e.locked_until - now if (e is not None and e.locked_until > now) else 0.0


def _bump(store: dict, key, free: int, now: float) -> float:
    e = store.get(key)
    if e is None:
        if len(store) >= _MAX_ENTRIES:
            for k in sorted(store, key=lambda k: store[k].last)[: _MAX_ENTRIES // 2]:
                del store[k]
        e = store[key] = _E()
    elif now - e.last > _DECAY_SECS:
        # Quiet period elapsed — forget prior failures so a persistent-but-legit
        # fat-fingering user never accumulates into a permanent lockout.
        e.failures = 0
        e.locked_until = 0.0
    e.failures += 1
    e.last = now
    if e.failures > free:
        step = min(e.failures - free - 1, len(_BACKOFF) - 1)
        e.locked_until = now + _BACKOFF[step]
        return _BACKOFF[step]
    return 0.0


def reserve(slug: str, ip: str) -> float:
    """Count one attempt BEFORE verification (concurrency-safe).

    Returns >0 (seconds to wait) ONLY when a *prior* attempt already locked this
    key — the caller must then reject without running bcrypt. Otherwise it counts
    this attempt and returns 0.0 so the caller proceeds to verify. If this very
    attempt is the one that crosses the budget, the lock is set for FOLLOWERS,
    but this attempt still gets its chance: a correct password on the boundary
    attempt succeeds and `release()` clears the lock, so a legitimate owner is
    never permanently shut out (the lock only bites the next wrong attempt).
    """
    now = time.monotonic()
    w = max(_wait(_by_ip.get((slug, ip)), now), _wait(_by_slug.get(slug), now))
    if w > 0.0:
        return w
    # Not currently locked — count this attempt. _bump may set locked_until for
    # followers; we still return 0.0 so THIS attempt reaches verification.
    _bump(_by_ip, (slug, ip), _FREE_IP, now)
    _bump(_by_slug, slug, _FREE_SLUG, now)
    return 0.0


def release(slug: str, ip: str) -> None:
    """A verified-correct password — clear the lock so a correct entry always
    recovers, even on the boundary attempt that just set it. The per-(slug, ip)
    counter is dropped entirely (the caller proved themselves); the per-slug
    backstop is relaxed by one and unlocked (legitimate activity resets the
    brute-force pressure — the holder has the password anyway)."""
    _by_ip.pop((slug, ip), None)
    se = _by_slug.get(slug)
    if se is not None:
        se.failures = max(0, se.failures - 1)
        se.locked_until = 0.0


def _reset_for_tests() -> None:  # pragma: no cover - test hook
    _by_ip.clear()
    _by_slug.clear()
