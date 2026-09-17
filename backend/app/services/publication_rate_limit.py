"""In-memory throttle for public-publication password attempts (publish-hardening F2).

A module-global dict counts attempts. It resets on restart — acceptable, because
bcrypt (~250 ms/try, offloaded) *plus* this lockout make online brute force of a
share password infeasible, and a restart is far rarer than a lockout window.
Redis is optional here (only the event stream uses it), so we deliberately do NOT
depend on it.

**This was written for a single-replica backend, and the backend is no longer
one.** Each process counts independently, so with N replicas an attacker gets up
to N times the thresholds below before backing off — the numbers are per process,
not per deployment. Nothing here detects that; the limits simply widen.

That is tolerable today only because the exposure is empty: the throttle guards
password-protected publications, and there are none. It stops being tolerable the
moment one exists, which is the trigger for moving these counters into shared
storage rather than a rewrite anyone should do speculatively.

Sharing them is not automatically the safer side, which is the other half of why
this still counts in memory. What it buys is one thing — thresholds that stop
widening with the replica count. What it costs, and what a port has to answer
for:

  - **It puts a dependency on a public request path.** `reserve()` is currently a
    dict operation that cannot fail or stall; it runs before bcrypt on every
    attempt. Backed by a network store it must choose between failing open (the
    protection disappears exactly when infrastructure is degraded) and failing
    closed (a blip becomes "nobody can open a protected publication"). Note that
    Redis is deliberately NOT on any request path here — `events_publisher` is a
    background drain and says so: PG stays the source of truth and an outage just
    accumulates rows. A throttle is a thin reason to cross that line.
  - **A naive port is worse than per-process.** The atomicity the first design
    note relies on comes free from the event loop: read-and-bump cannot
    interleave. Distributed, read-then-write reopens exactly the race that note
    exists to close. It has to be an atomic increment-and-test — Redis
    `INCR`/Lua, or an `UPSERT ... RETURNING`.
  - **It turns the throttle into an amplifier.** One attacker request becomes one
    round trip to the shared store, and `_by_slug` is a single key per
    publication, so a flood concentrates on one hot key — row-lock contention on
    a public path if that store is PostgreSQL.
  - **Decay and eviction have to be rebuilt.** `_DECAY_SECS` and `_MAX_ENTRIES`
    live in one place today. Getting their replacement wrong gives back either
    the permanent lockout the decay exists to prevent, or unbounded growth.
  - **Restart stops being an escape hatch.** A wrong lockout — from a bug or a
    misbehaving client — currently dies with the process. Shared and durable, it
    survives, so an operator needs a way to clear one.
  - **Tightening has its own false-lockout cost.** The per-slug backstop is
    effectively 30xN today and would become a real 30. That is the point, but a
    link opened by several people who each mistype once trips it sooner than it
    does now, and part of why false lockouts are rare today is that looseness.

If it is done: prefer PostgreSQL over Redis. It is already a request-path
dependency, so no new availability coupling appears; `UPSERT ... RETURNING`
gives the atomicity above directly; and the decay logic ports almost unchanged
as a comparison on stored `last` / `locked_until` columns. The hot-key
contention is the one cost no store avoids.

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


def _evict(store: dict, now: float) -> None:
    """Make room in a full map WITHOUT dropping active locks. Otherwise an
    attacker could flood junk slugs to force eviction of a currently-locked
    victim and reopen brute force (Codex reproduced this: 20k junk slugs evicted
    the locked entry and the next reserve() returned unlocked). So evict the
    oldest UNLOCKED entries first; only if the map is somehow entirely locked do
    we fall back to oldest-overall as a last resort to bound memory."""
    unlocked = [k for k, v in store.items() if v.locked_until <= now]
    victims = sorted(unlocked, key=lambda k: store[k].last)[: _MAX_ENTRIES // 2]
    if not victims:  # pathological: every entry locked — bound memory anyway
        victims = sorted(store, key=lambda k: store[k].last)[: _MAX_ENTRIES // 2]
    for k in victims:
        del store[k]


def _bump(store: dict, key, free: int, now: float) -> float:
    e = store.get(key)
    if e is None:
        if len(store) >= _MAX_ENTRIES:
            _evict(store, now)
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
        # Locked (per-ip or the per-slug backstop). The caller rejects with 429 —
        # UNLESS it's the authenticated owner, who is handled OUTSIDE this limiter
        # (an anonymous flood can lock the per-slug backstop, but the owner's AKB
        # session lets the route bypass the throttle entirely — see
        # _attempt_password_resolve — so a flood can't shut the owner out).
        return w
    # Not locked — count this attempt. _bump may set locked_until for followers;
    # we still return 0.0 so THIS attempt reaches verification.
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
