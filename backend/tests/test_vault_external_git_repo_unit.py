"""Unit tests for VaultExternalGitRepository state-transition writes.
No DB: a capture pool records the executed SQL + args so we can pin
that each method writes the hardened columns (sync_state / poll_next_at /
poll_retry_count) and NEVER the legacy rollout-fence columns (next_attempt_at /
retry_count), plus the type-enforced safe-failure boundary.
"""

from __future__ import annotations

import uuid

import pytest

from app.repositories.vault_external_git_repo import VaultExternalGitRepository


class _CaptureConn:
    def __init__(self, status: str = "UPDATE 1"):
        self.executed: list[tuple[str, tuple]] = []
        self._status = status

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        # asyncpg returns a command tag ("UPDATE <n>"); the repo parses it to
        # detect a stale expected-state CAS (0 rows). Default "UPDATE 1" = matched.
        return self._status


class _Acq:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


class _CapturePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acq(self._conn)


def _repo(status: str = "UPDATE 1"):
    conn = _CaptureConn(status)
    return VaultExternalGitRepository(_CapturePool(conn)), conn


def _no_legacy_columns(sql: str) -> None:
    """Assert the SQL touches neither legacy fence column. ``poll_retry_count``
    legitimately contains the substring ``retry_count``, so require every
    ``retry_count`` occurrence to be part of ``poll_retry_count``; ``poll_next_at``
    never contains ``next_attempt_at``."""
    assert "next_attempt_at" not in sql
    assert sql.count("retry_count") == sql.count("poll_retry_count")


@pytest.mark.asyncio
async def test_mark_success_no_sha_uses_hardened_columns():
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.mark_success(vid, 300)
    assert ok is True  # CAS matched (command tag "UPDATE 1")
    sql, args = conn.executed[0]
    assert "poll_retry_count  = 0" in sql
    assert "poll_next_at" in sql
    assert "last_synced_sha" not in sql  # no-sha branch: cursor not advanced
    # Expected-state CAS: only a still-'active' row is updated, so a
    # reconcile that finished on a since-quarantined row is a no-op.
    assert "AND sync_state = 'active'" in sql
    _no_legacy_columns(sql)
    assert args == (vid, "300")


@pytest.mark.asyncio
async def test_mark_success_with_sha_advances_cursor():
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.mark_success(vid, 600, new_sha="deadbeef")
    assert ok is True
    sql, args = conn.executed[0]
    assert "last_synced_sha" in sql and "last_synced_at" in sql
    assert "poll_retry_count  = 0" in sql
    assert "AND sync_state = 'active'" in sql  # expected-state CAS
    _no_legacy_columns(sql)
    assert args == (vid, "deadbeef", "600")


@pytest.mark.asyncio
async def test_mark_failure_increments_hardened_retry_and_backs_off():
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.mark_failure(
        vid, "transient", "temporarily unavailable", 60, "pending_preflight"
    )
    assert ok is True
    sql, args = conn.executed[0]
    assert "poll_retry_count  = poll_retry_count + 1" in sql
    assert "poll_next_at" in sql
    set_clause, where_clause = sql.split("WHERE", 1)
    # sync_state is NOT set (state unchanged) but IS the expected-state CAS guard
    # in the WHERE (caller passes 'pending_preflight' / 'active').
    assert "sync_state" not in set_clause
    assert "AND sync_state = $4" in where_clause
    _no_legacy_columns(sql)
    assert args == (vid, "[transient] temporarily unavailable", "60", "pending_preflight")


@pytest.mark.asyncio
async def test_mark_failure_rejects_non_str_code():
    repo, _ = _repo()
    with pytest.raises(TypeError):
        await repo.mark_failure(uuid.uuid4(), object(), "msg", 60, "active")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mark_failure_rejects_non_str_expected_state():
    repo, _ = _repo()
    with pytest.raises(TypeError):
        await repo.mark_failure(uuid.uuid4(), "transient", "msg", 60, object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_activate_from_preflight_promotes_and_scrubs():
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.activate_from_preflight(
        vid, "https://github.com/o/r.git", "ghp_tok", 300
    )
    assert ok is True
    sql, args = conn.executed[0]
    assert "sync_state        = 'active'" in sql
    assert "sync_state_reason = NULL" in sql
    assert "poll_next_at      = NOW()" in sql  # immediate reconcile
    assert "poll_retry_count  = 0" in sql
    assert "remote_url" in sql and "auth_token" in sql
    # Expected-state CAS: activate only from 'pending_preflight', so a
    # slow preflight cannot resurrect a since-quarantined row.
    assert "AND sync_state = 'pending_preflight'" in sql
    _no_legacy_columns(sql)
    assert args == (vid, "https://github.com/o/r.git", "ghp_tok")


@pytest.mark.asyncio
async def test_scrub_legacy_credential_persists_credential_free_url():
    """The pre-DNS scrub writes a credential-free remote_url +
    migrated token, guarded by an OLD-VALUE CAS on the CLAIMED (remote_url,
    auth_token) — state-independent, never touching sync_state or the legacy
    fence. Being state-independent, it scrubs even a row an operator quarantined
    mid-flight; the old-value predicate is what stops it clobbering a newer
    operator config (a 0-row no-op)."""
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.scrub_legacy_credential(
        vid, "https://github.com/o/r.git", "ghp_tok",
        "https://x-access-token:ghp_tok@github.com/o/r.git", None,  # pragma: allowlist secret
    )
    assert ok is True
    sql, args = conn.executed[0]
    assert "remote_url = $2" in sql and "auth_token = $3" in sql
    # OLD-VALUE CAS: guarded on the claimed remote_url ($4) + auth_token ($5, via
    # IS NOT DISTINCT FROM so a NULL token still matches), NOT on sync_state — so
    # it also scrubs a since-quarantined row.
    assert "AND remote_url = $4" in sql
    assert "AND auth_token IS NOT DISTINCT FROM $5" in sql
    assert "sync_state" not in sql  # neither read nor written — pure hygiene write
    _no_legacy_columns(sql)
    assert args == (
        vid, "https://github.com/o/r.git", "ghp_tok",
        "https://x-access-token:ghp_tok@github.com/o/r.git", None,  # pragma: allowlist secret
    )


@pytest.mark.asyncio
async def test_cas_transitions_return_false_when_row_superseded():
    """Every CAS write returns False when it matches zero rows. For the
    expected-state CAS methods the row was quarantined (or advanced) out from
    under the in-flight op; for the old-value-CAS scrub the stored URL/token
    changed under it. Either way the write is stale and must be discarded, not
    counted."""
    vid = uuid.uuid4()
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.activate_from_preflight(vid, "https://h/r.git", None, 300) is False
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.mark_success(vid, 300) is False
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.mark_success(vid, 300, new_sha="deadbeef") is False
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.mark_failure(vid, "transient", "x", 60, "active") is False
    # Old-value CAS scrub: 0 rows when the claimed old URL/token no longer match.
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.scrub_legacy_credential(
        vid, "https://h/r.git", None, "https://tok@h/r.git", None
    ) is False
    # An already-quarantined row: re-quarantine is a 0-row no-op (reason kept) —
    # neither expected state ('pending_preflight' here) equals 'quarantined'.
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.quarantine(vid, "legacy_credential", "pending_preflight") is False


@pytest.mark.asyncio
async def test_quarantine_sets_state_and_secret_free_reason():
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.quarantine(vid, "policy_violation", "active")
    assert ok is True
    sql, args = conn.executed[0]
    assert "sync_state        = 'quarantined'" in sql
    assert "sync_state_reason = $2" in sql
    # Exact expected-state CAS: only the state the
    # caller CLAIMED the row in (here 'active', bound as $4) transitions, so an
    # already-quarantined OR since-reset row is a no-op that preserves the first
    # reason. NOT the old "IN (pending, active)".
    assert "AND sync_state = $4" in sql
    assert "IN ('pending_preflight', 'active')" not in sql
    # The reason is a bound PARAMETER (never interpolated) and secret-free; the
    # expected_state is likewise a bound param.
    assert args == (vid, "policy_violation", "[quarantine] policy_violation", "active")


@pytest.mark.asyncio
async def test_quarantine_rejects_non_str_reason():
    repo, _ = _repo()
    with pytest.raises(TypeError):
        await repo.quarantine(uuid.uuid4(), object(), "active")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_quarantine_rejects_non_str_expected_state():
    repo, _ = _repo()
    with pytest.raises(TypeError):
        await repo.quarantine(  # type: ignore[arg-type]
            uuid.uuid4(), "policy_violation", object()
        )


# ── snapshot-CAS old-value guard ──
# When a transition is passed the (validated_url, validated_token) the caller
# checked, the WHERE gains an old-value guard so a stale validation can neither
# clobber nor terminalize a config an operator reconfigured mid-flight — a 0-row
# no-op. Omitting it keeps the pure expected-state CAS the reconcile path uses.
@pytest.mark.asyncio
async def test_activate_snapshot_cas_adds_old_value_guard():
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.activate_from_preflight(
        vid, "https://h/canonical.git", "ghp_tok", 300,
        validated_url="https://h/scrubbed.git", validated_token="ghp_tok",
    )
    assert ok is True
    sql, args = conn.executed[0]
    assert "AND sync_state = 'pending_preflight'" in sql
    # The snapshot old-value guard is present, NUMBERED after the base params.
    assert "AND remote_url = $4" in sql
    assert "AND auth_token IS NOT DISTINCT FROM $5" in sql
    _no_legacy_columns(sql)
    assert args == (
        vid, "https://h/canonical.git", "ghp_tok",
        "https://h/scrubbed.git", "ghp_tok",
    )


@pytest.mark.asyncio
async def test_activate_without_snapshot_is_state_only():
    """Omitting validated_url keeps activate a pure expected-state CAS (no
    old-value guard, no extra params) — the form older call sites rely on."""
    repo, conn = _repo()
    vid = uuid.uuid4()
    await repo.activate_from_preflight(vid, "https://h/r.git", None, 300)
    sql, args = conn.executed[0]
    assert "remote_url = $4" not in sql
    assert "IS NOT DISTINCT FROM" not in sql
    assert args == (vid, "https://h/r.git", None)


@pytest.mark.asyncio
async def test_quarantine_snapshot_cas_adds_old_value_guard():
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.quarantine(
        vid, "policy_violation", "pending_preflight",
        validated_url="https://h/r.git", validated_token=None,
    )
    assert ok is True
    sql, args = conn.executed[0]
    # expected_state is bound as $4; the snapshot old-value guard NUMBERS after it.
    assert "AND sync_state = $4" in sql
    assert "AND remote_url = $5" in sql
    assert "AND auth_token IS NOT DISTINCT FROM $6" in sql
    assert args == (
        vid, "policy_violation", "[quarantine] policy_violation", "pending_preflight",
        "https://h/r.git", None,
    )


@pytest.mark.asyncio
async def test_mark_failure_snapshot_cas_adds_old_value_guard():
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.mark_failure(
        vid, "transient", "temporarily unavailable", 60, "pending_preflight",
        validated_url="https://h/r.git", validated_token="ghp_tok",
    )
    assert ok is True
    sql, args = conn.executed[0]
    assert "AND sync_state = $4" in sql
    assert "AND remote_url = $5" in sql
    assert "AND auth_token IS NOT DISTINCT FROM $6" in sql
    assert args == (
        vid, "[transient] temporarily unavailable", "60", "pending_preflight",
        "https://h/r.git", "ghp_tok",
    )


@pytest.mark.asyncio
async def test_mark_success_snapshot_cas_adds_old_value_guard():
    # no-sha branch: base params are $1,$2 → guard is $3,$4.
    repo, conn = _repo()
    vid = uuid.uuid4()
    await repo.mark_success(
        vid, 300, validated_url="https://h/r.git", validated_token=None,
    )
    sql, args = conn.executed[0]
    assert "AND sync_state = 'active'" in sql
    assert "AND remote_url = $3" in sql
    assert "AND auth_token IS NOT DISTINCT FROM $4" in sql
    assert args == (vid, "300", "https://h/r.git", None)
    # sha branch: base params are $1,$2,$3 → guard shifts to $4,$5.
    repo, conn = _repo()
    await repo.mark_success(
        vid, 600, new_sha="deadbeef",
        validated_url="https://h/r.git", validated_token="ghp_tok",
    )
    sql, args = conn.executed[0]
    assert "AND remote_url = $4" in sql
    assert "AND auth_token IS NOT DISTINCT FROM $5" in sql
    assert args == (vid, "deadbeef", "600", "https://h/r.git", "ghp_tok")


@pytest.mark.asyncio
async def test_snapshot_cas_returns_false_when_config_changed():
    """A snapshot-CAS write returns False when it matches 0 rows — the row was
    reconfigured (old value gone) or moved to a terminal state under it."""
    vid = uuid.uuid4()
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.activate_from_preflight(
        vid, "https://h/c.git", None, 300,
        validated_url="https://h/s.git", validated_token=None,
    ) is False
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.quarantine(
        vid, "policy_violation", "pending_preflight",
        validated_url="https://h/s.git", validated_token=None,
    ) is False
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.mark_failure(
        vid, "transient", "x", 60, "pending_preflight",
        validated_url="https://h/s.git", validated_token=None,
    ) is False
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.mark_success(
        vid, 300, validated_url="https://h/s.git", validated_token=None,
    ) is False


# ── atomic malformed redact + quarantine ──────────
@pytest.mark.asyncio
async def test_redact_malformed_and_quarantine_is_atomic_and_credential_free():
    """A SINGLE UPDATE redacts remote_url → sentinel, auth_token → NULL, AND moves
    the row to 'quarantined' with a fixed reason, guarded on the claimed old
    (url, token) + a pending/active state. One indivisible
    write, so a reconfigure can't slip a new credential between a separate redact
    and quarantine."""
    from app.repositories.vault_external_git_repo import _MALFORMED_URL_SENTINEL
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.redact_malformed_and_quarantine(
        vid, "https://x-access-token:ghp_leak@[::1", None, "malformed_url",  # pragma: allowlist secret
    )
    assert ok is True
    sql, args = conn.executed[0]
    # remote_url → sentinel ($2), auth_token → NULL, sync_state → quarantined, ALL
    # in one statement.
    assert "remote_url        = $2" in sql
    assert "auth_token        = NULL" in sql
    assert "sync_state        = 'quarantined'" in sql
    assert "sync_state_reason = $3" in sql
    # Old-value CAS + exact 'pending_preflight' state guard (malformed is a
    # preflight-only path, consistent with quarantine's exact expected-state CAS).
    assert "AND remote_url = $5" in sql
    assert "AND auth_token IS NOT DISTINCT FROM $6" in sql
    assert "AND sync_state = 'pending_preflight'" in sql
    _no_legacy_columns(sql)
    # $2 (the value WRITTEN to remote_url) is the fixed credential-free sentinel,
    # never the raw claimed URL ($5); the reason ($3) is the fixed enum.
    assert args == (
        vid, _MALFORMED_URL_SENTINEL, "malformed_url",
        "[quarantine] malformed_url",
        "https://x-access-token:ghp_leak@[::1", None,  # pragma: allowlist secret
    )
    assert "ghp_leak" not in _MALFORMED_URL_SENTINEL


@pytest.mark.asyncio
async def test_redact_malformed_and_quarantine_rejects_non_str_reason():
    repo, _ = _repo()
    with pytest.raises(TypeError):
        await repo.redact_malformed_and_quarantine(
            uuid.uuid4(), "https://[::1", None, object(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_redact_malformed_and_quarantine_false_when_superseded():
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.redact_malformed_and_quarantine(
        uuid.uuid4(), "https://[::1", None, "malformed_url",
    ) is False


@pytest.mark.asyncio
async def test_redact_malformed_url_if_quarantined_preserves_reason():
    """Hygiene: on an ALREADY-quarantined row with the same old value, strip the
    stranded credential (remote_url → sentinel, auth_token → NULL) WITHOUT touching
    sync_state_reason / sync_state_at — the first quarantine reason is preserved."""
    from app.repositories.vault_external_git_repo import _MALFORMED_URL_SENTINEL
    repo, conn = _repo()
    vid = uuid.uuid4()
    ok = await repo.redact_malformed_url_if_quarantined(
        vid, "https://x-access-token:ghp_leak@[::1", None,  # pragma: allowlist secret
    )
    assert ok is True
    sql, args = conn.executed[0]
    assert "remote_url = $2" in sql and "auth_token = NULL" in sql
    # Only an already-quarantined row, and it does NOT rewrite reason/timestamp.
    assert "AND sync_state = 'quarantined'" in sql
    assert "sync_state_reason" not in sql
    assert "sync_state_at" not in sql
    assert "AND remote_url = $3" in sql
    assert "AND auth_token IS NOT DISTINCT FROM $4" in sql
    assert args == (
        vid, _MALFORMED_URL_SENTINEL,
        "https://x-access-token:ghp_leak@[::1", None,  # pragma: allowlist secret
    )


@pytest.mark.asyncio
async def test_redact_malformed_url_if_quarantined_false_when_no_match():
    repo, _ = _repo(status="UPDATE 0")
    assert await repo.redact_malformed_url_if_quarantined(
        uuid.uuid4(), "https://[::1", None,
    ) is False
