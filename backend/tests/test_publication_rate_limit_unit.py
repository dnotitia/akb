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


def test_per_slug_backstop_survives_ip_rotation():
    rl._reset_for_tests()
    slug = "slug4"
    for i in range(rl._FREE_SLUG + 1):
        assert rl.reserve(slug, f"10.0.0.{i}") == 0.0
    # Backstop crossed → any ip is now locked.
    assert rl.reserve(slug, "203.0.113.1") > 0.0


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
