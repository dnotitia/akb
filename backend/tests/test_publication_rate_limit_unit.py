"""Unit tests for the publish-hardening F2 password-attempt throttle.

Pure in-memory logic — no DB, no network. Runs in `pytest -k 'not _e2e'`.
"""
import time

from app.services import publication_rate_limit as rl


def test_crossing_attempt_proceeds_but_followers_are_locked():
    rl._reset_for_tests()
    slug, ip = "slug1", "1.2.3.4"
    # The first FREE_IP+1 attempts all PROCEED (return 0). The (FREE_IP+1)-th
    # crosses the budget and sets the lock for FOLLOWERS, but itself still gets
    # a chance to verify (so a correct password on the boundary isn't shut out).
    for _ in range(rl._FREE_IP + 1):
        assert rl.reserve(slug, ip) == 0.0
    # The next (follower) attempt is rejected before bcrypt.
    assert rl.reserve(slug, ip) > 0.0


def test_correct_password_on_boundary_recovers_via_release():
    rl._reset_for_tests()
    slug, ip = "slug2", "1.1.1.1"
    for _ in range(rl._FREE_IP + 1):  # boundary crossed; lock set for followers
        assert rl.reserve(slug, ip) == 0.0
    assert rl.reserve(slug, ip) > 0.0          # a follower would be locked
    rl.release(slug, ip)                        # ...but the boundary password was CORRECT
    assert rl.reserve(slug, ip) == 0.0          # fully recovered, no lockout


def test_wrong_password_keeps_the_lock():
    rl._reset_for_tests()
    slug, ip = "slug3", "2.2.2.2"
    for _ in range(rl._FREE_IP + 1):
        rl.reserve(slug, ip)
    # No release() (boundary password was wrong) → the lock stands.
    assert rl.reserve(slug, ip) > 0.0


def test_per_slug_backstop_throttles_ip_rotation():
    rl._reset_for_tests()
    slug = "slug4"
    for i in range(rl._FREE_SLUG + 1):
        assert rl.reserve(slug, f"10.0.0.{i}") == 0.0
    # Backstop crossed. A fresh IP is now throttled with NO anonymous probe path —
    # the limiter deliberately has no "let one attempt through" escape hatch, since
    # (per the G5 review) a probe accelerates online brute force and doesn't even
    # guarantee the owner wins the race. Owner recovery is handled ABOVE the
    # limiter: an authenticated publication-manager session bypasses the throttle
    # entirely in _attempt_password_resolve (see test_publications_e2e.sh G5).
    assert rl.reserve(slug, "203.0.113.1") > 0.0    # fresh rotating IP is throttled
    assert rl.reserve(slug, "203.0.113.2") > 0.0    # and so is the next one


def test_per_slug_backstop_lock_is_not_permanent():
    # The backstop lock is not a permanent shut-out even for anonymous callers: a
    # quiet period (_DECAY_SECS) elapsing forgets the accumulated failures, so the
    # slug is usable again without a restart. (An authenticated owner never waits
    # for this — they bypass the throttle in the route — but the decay guarantees
    # the anonymous lock self-heals too.)
    rl._reset_for_tests()
    slug = "victim"
    for i in range(rl._FREE_SLUG + 2):            # anonymous flood, rotating IPs
        rl.reserve(slug, f"9.9.9.{i}")
    se = rl._by_slug[slug]
    assert se.locked_until > time.monotonic()     # backstop is locked now
    # Simulate a quiet hour passing: the short backoff lock has since expired AND
    # the last attempt is older than _DECAY_SECS, so _bump forgets the failures.
    se.locked_until = time.monotonic() - 1
    se.last = time.monotonic() - rl._DECAY_SECS - 10
    assert rl.reserve(slug, "203.0.113.9") == 0.0      # decayed → usable again
    assert se.failures == 1                             # counter reset, then counted once


def test_quiet_period_decays_the_counter():
    rl._reset_for_tests()
    slug, ip = "slug5", "3.3.3.3"
    for _ in range(rl._FREE_IP):
        rl.reserve(slug, ip)
    e = rl._by_ip[(slug, ip)]
    e.last = time.monotonic() - rl._DECAY_SECS - 10   # simulate a quiet hour
    assert rl.reserve(slug, ip) == 0.0
    assert e.failures == 1                             # decayed then counted once


def test_independent_slugs_do_not_interfere():
    rl._reset_for_tests()
    ip = "1.1.1.1"
    for _ in range(rl._FREE_IP + 2):
        rl.reserve("locked-slug", ip)
    assert rl.reserve("locked-slug", ip) > 0.0
    assert rl.reserve("other-slug", ip) == 0.0


def test_capacity_eviction_preserves_locked_victim():
    # A flood of junk slugs must not evict a currently-LOCKED victim from the
    # per-slug backstop (that would reopen brute force). Lock a victim, then
    # overflow the map with unlocked junk and confirm the victim's lock survives.
    rl._reset_for_tests()
    victim = "victim-slug"
    for i in range(rl._FREE_SLUG + 2):        # trip the per-slug backstop lock
        rl.reserve(victim, f"7.7.7.{i}")
    assert rl._by_slug[victim].locked_until > time.monotonic()
    # Overflow the per-slug map with unlocked junk to force eviction (one touch
    # each → below the free budget → never locked).
    for i in range(rl._MAX_ENTRIES + 10):
        rl.reserve(f"junk-{i}", "8.8.8.8")
    assert len(rl._by_slug) <= rl._MAX_ENTRIES         # map stayed bounded
    assert victim in rl._by_slug                        # ...but the lock was NOT evicted
    assert rl._by_slug[victim].locked_until > time.monotonic()
