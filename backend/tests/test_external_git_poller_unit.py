"""Unit tests for the external_git quarantine state machine.

No network, no DB: the shared validator is monkeypatched, the repository is a
call-recording fake, and the claim SQL is captured via a fake connection. The
rollout-fence proof is a pure simulation of the pre-hardening poller's claim
predicate against a post-migration (fenced) row — the DB-backed apply + real
old-SQL proof lives in ``test_migration_049_quarantine_unit.py``.
"""

from __future__ import annotations

import datetime as dt
import types
from pathlib import Path

import pytest

from app.services import external_git_poller as poller
from app.services import external_git_service as egs
from app.services.external_git_validation import (
    ExternalGitPolicyError,
    ExternalGitTransientError,
)


# ── Call-recording fakes ─────────────────────────────────────────────
class FakeRepo:
    """Records repo calls in order. Each CAS method returns a bool (True = a row
    was written); a ``False`` simulates a snapshot-CAS that matched 0 rows because
    the row was moved to a terminal state OR the stored (remote_url, auth_token)
    changed under an in-flight op.

    The bool for each method comes from its ``*_results`` list (popped per
    successive call) when given, else its single ``*_ok`` default. ``rows`` backs
    ``get`` for the snapshot-CAS reload path (the poller reloads + re-classifies
    the CURRENT row on a 0-row miss). The recorded call tuples for the state
    transitions carry the snapshot old-value ``(validated_url, validated_token)``
    the poller passed (``None, None`` on the reconcile path, which is state-only).
    """

    def __init__(
        self,
        *,
        activate_ok: bool = True,
        quarantine_ok: bool = True,
        mark_failure_ok: bool = True,
        redact_mq_ok: bool = True,
        redact_hygiene_ok: bool = False,
        scrub_results: list[bool] | None = None,
        activate_results: list[bool] | None = None,
        quarantine_results: list[bool] | None = None,
        rows: dict | None = None,
    ):
        self.calls: list[tuple] = []
        self._activate_ok = activate_ok
        self._quarantine_ok = quarantine_ok
        self._mark_failure_ok = mark_failure_ok
        self._redact_mq_ok = redact_mq_ok
        self._redact_hygiene_ok = redact_hygiene_ok
        self._scrub_results = list(scrub_results) if scrub_results is not None else None
        self._activate_results = (
            list(activate_results) if activate_results is not None else None
        )
        self._quarantine_results = (
            list(quarantine_results) if quarantine_results is not None else None
        )
        self._rows = rows or {}

    @staticmethod
    def _next(results: list[bool] | None, default: bool) -> bool:
        return results.pop(0) if results else default

    async def scrub_legacy_credential(
        self, vault_id, scrubbed_url, new_auth_token, claimed_url, claimed_token
    ):
        self.calls.append(
            ("scrub", vault_id, scrubbed_url, new_auth_token, claimed_url, claimed_token)
        )
        return self._next(self._scrub_results, True)

    async def get(self, vault_id):
        self.calls.append(("get", vault_id))
        return self._rows.get(vault_id)

    async def activate_from_preflight(
        self, vault_id, canonical_url, auth_token, poll_interval_secs,
        *, validated_url=None, validated_token=None,
    ):
        self.calls.append(
            ("activate", vault_id, canonical_url, auth_token, poll_interval_secs,
             validated_url, validated_token)
        )
        return self._next(self._activate_results, self._activate_ok)

    async def quarantine(
        self, vault_id, reason_code, expected_state,
        *, validated_url=None, validated_token=None,
    ):
        self.calls.append(
            ("quarantine", vault_id, reason_code, expected_state,
             validated_url, validated_token)
        )
        return self._next(self._quarantine_results, self._quarantine_ok)

    async def mark_failure(
        self, vault_id, code, safe_message, backoff_secs, expected_state,
        *, validated_url=None, validated_token=None,
    ):
        self.calls.append(
            ("mark_failure", vault_id, code, safe_message, backoff_secs, expected_state,
             validated_url, validated_token)
        )
        return self._mark_failure_ok

    async def mark_success(self, *args, **kwargs):
        self.calls.append(("mark_success", args, kwargs))
        return True

    async def redact_malformed_and_quarantine(
        self, vault_id, claimed_url, claimed_token, reason_code
    ):
        self.calls.append(
            ("redact_mq", vault_id, claimed_url, claimed_token, reason_code)
        )
        return self._redact_mq_ok

    async def redact_malformed_url_if_quarantined(self, vault_id, claimed_url, claimed_token):
        self.calls.append(("redact_hygiene", vault_id, claimed_url, claimed_token))
        return self._redact_hygiene_ok


class CaptureConn:
    """Records the SQL passed to fetchrow; returns no row (claim → None)."""

    def __init__(self):
        self.sql: str | None = None
        self.args: tuple = ()

    async def fetchrow(self, sql, *args):
        self.sql = sql
        self.args = args
        return None


def _preflight_claim(**overrides) -> dict:
    base = {
        "vault_id": "v-1",
        "vault_name": "mirror-1",
        "remote_url": "https://github.com/o/r.git",
        "remote_branch": "main",
        "auth_token": None,
        "poll_interval_secs": 300,
        "poll_retry_count": 0,
    }
    base.update(overrides)
    return base


def _reload_row(**overrides) -> dict:
    """A ``vault_external_git`` row as ``ext_repo.get()`` returns it (SELECT *),
    for the snapshot-CAS reload path. Defaults to a clean,
    credential-free, still-pending row; override any field for a specific race."""
    base = {
        "remote_url": "https://github.com/o/r.git",
        "auth_token": None,
        "remote_branch": "main",
        "poll_interval_secs": 300,
        "poll_retry_count": 0,
        "sync_state": "pending_preflight",
    }
    base.update(overrides)
    return base


# ── _scrub_userinfo (pure) ───────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, expect_url, expect_token, expect_ok",
    [
        # No userinfo → passthrough (validator canonicalizes).
        ("https://github.com/o/r.git", "https://github.com/o/r.git", None, True),
        # The one supported form: x-access-token:TOKEN → token lifted out.
        (
            "https://x-access-token:ghp_abc@github.com/o/r.git",  # pragma: allowlist secret
            "https://github.com/o/r.git",
            "ghp_abc",
            True,
        ),
        # Port + query preserved (so the validator still rejects the query later).
        (
            "https://x-access-token:tok@host:8443/p?ref=1",  # pragma: allowlist secret
            "https://host:8443/p?ref=1",
            "tok",
            True,
        ),
        # IPv6 literal brackets survive the netloc surgery.
        (
            "https://x-access-token:tok@[2001:db8::1]:443/r.git",  # pragma: allowlist secret
            "https://[2001:db8::1]:443/r.git",
            "tok",
            True,
        ),
        # A colon inside the token is preserved (partition on the FIRST ':').
        (
            "https://x-access-token:to:k@github.com/o/r.git",  # pragma: allowlist secret
            "https://github.com/o/r.git",
            "to:k",
            True,
        ),
        # Unscrubbable userinfo forms → not migratable, BUT the returned URL is
        # still credential-free (userinfo stripped) so the caller persists it and
        # leaves no secret in remote_url before quarantining.
        ("https://user:pass@github.com/o/r.git", "https://github.com/o/r.git", None, False),  # pragma: allowlist secret
        ("https://baretoken@github.com/o/r.git", "https://github.com/o/r.git", None, False),
        ("https://oauth2:tok@gitlab.com/o/r.git", "https://gitlab.com/o/r.git", None, False),  # pragma: allowlist secret
    ],
)
def test_scrub_userinfo(raw, expect_url, expect_token, expect_ok):
    url, token, ok = poller._scrub_userinfo(raw)
    assert (url, token, ok) == (expect_url, expect_token, expect_ok)


def test_scrub_userinfo_raises_on_malformed_url():
    """A URL urlsplit cannot parse (bad IPv6 literal) is a PERMANENT syntax
    violation → ExternalGitPolicyError → quarantine, not an infinite backoff.
    The message is fixed and secret-free."""
    with pytest.raises(ExternalGitPolicyError) as ei:
        poller._scrub_userinfo("https://[::1")
    # Fixed message only — no fragment of the (possibly credential-bearing) URL.
    assert "malformed" in str(ei.value)
    assert "::1" not in str(ei.value)


# ── preflight transitions ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_preflight_activates_on_valid(monkeypatch):
    captured: dict = {}

    def fake_validate(data, *, settings, resolve, resolver=None):
        captured["data"] = data
        captured["resolve"] = resolve
        return types.SimpleNamespace(canonical_url="https://github.com/o/r.git")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo()
    rc = await poller._run_preflight(_preflight_claim(), repo)

    assert rc == 1
    assert captured["resolve"] is True  # Layer-2 re-validation resolves DNS
    # poll_interval_secs is deliberately NOT re-validated at preflight.
    assert "poll_interval_secs" not in captured["data"]
    # activate is a snapshot-CAS: it carries the validated (remote_url, auth_token)
    # so a stale result can't clobber a config an operator reconfigured mid-flight.
    assert repo.calls == [
        ("activate", "v-1", "https://github.com/o/r.git", None, 300,
         "https://github.com/o/r.git", None),
    ]


@pytest.mark.asyncio
async def test_preflight_moves_url_token_to_column(monkeypatch):
    captured: dict = {}

    def fake_validate(data, *, settings, resolve, resolver=None):
        captured["data"] = data
        return types.SimpleNamespace(canonical_url="https://github.com/o/r.git")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo()
    claim = _preflight_claim(
        remote_url="https://x-access-token:ghp_zzz@github.com/o/r.git",  # pragma: allowlist secret
        auth_token=None,
    )
    await poller._run_preflight(claim, repo)

    # Token lifted from the URL into the column; validator saw the scrubbed URL.
    assert captured["data"]["remote_url"] == "https://github.com/o/r.git"
    assert captured["data"]["auth_token"] == "ghp_zzz"
    # The credential-free URL + token are persisted BEFORE validate via
    # the old-value CAS (claimed raw URL + claimed NULL token are the CAS guard),
    # then the row is activated with the SAME (scrubbed_url, token) snapshot guard.
    assert repo.calls == [
        ("scrub", "v-1", "https://github.com/o/r.git", "ghp_zzz",
         "https://x-access-token:ghp_zzz@github.com/o/r.git", None),  # pragma: allowlist secret
        ("activate", "v-1", "https://github.com/o/r.git", "ghp_zzz", 300,
         "https://github.com/o/r.git", "ghp_zzz"),
    ]


@pytest.mark.asyncio
async def test_preflight_column_token_wins_over_url_token(monkeypatch):
    captured: dict = {}

    def fake_validate(data, *, settings, resolve, resolver=None):
        captured["data"] = data
        return types.SimpleNamespace(canonical_url="https://github.com/o/r.git")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo()
    claim = _preflight_claim(
        remote_url="https://x-access-token:from_url@github.com/o/r.git",  # pragma: allowlist secret
        auth_token="from_column",
    )
    await poller._run_preflight(claim, repo)

    assert captured["data"]["auth_token"] == "from_column"
    # The column token wins and is what gets persisted by the pre-DNS scrub too;
    # the CAS guard carries the claimed raw URL + claimed column token, and the
    # activate snapshot guard carries (scrubbed_url, column token).
    assert repo.calls == [
        ("scrub", "v-1", "https://github.com/o/r.git", "from_column",
         "https://x-access-token:from_url@github.com/o/r.git", "from_column"),  # pragma: allowlist secret
        ("activate", "v-1", "https://github.com/o/r.git", "from_column", 300,
         "https://github.com/o/r.git", "from_column"),
    ]


@pytest.mark.asyncio
async def test_preflight_quarantines_on_policy_violation(monkeypatch):
    def fake_validate(data, *, settings, resolve, resolver=None):
        raise ExternalGitPolicyError("host resolves to a non-routable address (private)")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo()
    rc = await poller._run_preflight(_preflight_claim(), repo)

    assert rc == 1
    # The policy quarantine is a snapshot-CAS on the validated (remote_url, token),
    # expecting the exact 'pending_preflight' state it claimed the row in (fix-4).
    assert repo.calls == [
        ("quarantine", "v-1", poller._QUARANTINE_POLICY, "pending_preflight",
         "https://github.com/o/r.git", None),
    ]


@pytest.mark.asyncio
async def test_preflight_quarantines_unscrubbable_credential(monkeypatch):
    # Scrub fails BEFORE validate is reached — validate must not even be called.
    def boom(*a, **k):
        raise AssertionError("validate must not run for an unscrubbable URL")

    monkeypatch.setattr(poller, "validate", boom)
    repo = FakeRepo()
    claim = _preflight_claim(remote_url="https://user:pass@github.com/o/r.git")  # pragma: allowlist secret
    rc = await poller._run_preflight(claim, repo)

    assert rc == 1
    # The credential-free URL is persisted first (no user:pass left in
    # remote_url) via the old-value CAS, THEN the row is quarantined for manual
    # re-auth via a snapshot-CAS on the scrubbed (url, token).
    assert repo.calls == [
        ("scrub", "v-1", "https://github.com/o/r.git", None,
         "https://user:pass@github.com/o/r.git", None),  # pragma: allowlist secret
        ("quarantine", "v-1", poller._QUARANTINE_LEGACY_CREDENTIAL, "pending_preflight",
         "https://github.com/o/r.git", None),
    ]


@pytest.mark.asyncio
async def test_preflight_backs_off_on_transient(monkeypatch):
    def fake_validate(data, *, settings, resolve, resolver=None):
        raise ExternalGitTransientError("host resolution timed out")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo()
    rc = await poller._run_preflight(_preflight_claim(poll_retry_count=2), repo)

    assert rc == 0  # transient → nothing more to do this tick
    assert len(repo.calls) == 1
    kind, vault_id, code, _msg, backoff, expected, vurl, vtok = repo.calls[0]
    assert (kind, vault_id, code) == ("mark_failure", "v-1", "transient")
    assert backoff == poller.next_attempt_delay(2)
    # A preflight backoff stays 'pending_preflight' and carries the validated
    # (url, token) snapshot guard.
    assert expected == "pending_preflight"
    assert (vurl, vtok) == ("https://github.com/o/r.git", None)


# ── A credential-free remote_url is persisted on EVERY branch, and
#    every terminal write carries the validated (url, token) snapshot guard ──
@pytest.mark.asyncio
async def test_preflight_scrubs_before_policy_quarantine(monkeypatch):
    """An x-access-token URL that then fails a policy check is scrubbed in the DB
    FIRST — the token never lingers in remote_url after the quarantine."""
    def fake_validate(data, *, settings, resolve, resolver=None):
        raise ExternalGitPolicyError("host resolves to a non-routable address")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo()
    claim = _preflight_claim(
        remote_url="https://x-access-token:ghp_leak@github.com/o/r.git"  # pragma: allowlist secret
    )
    rc = await poller._run_preflight(claim, repo)

    assert rc == 1
    assert repo.calls == [
        ("scrub", "v-1", "https://github.com/o/r.git", "ghp_leak",
         "https://x-access-token:ghp_leak@github.com/o/r.git", None),  # pragma: allowlist secret
        ("quarantine", "v-1", poller._QUARANTINE_POLICY, "pending_preflight",
         "https://github.com/o/r.git", "ghp_leak"),
    ]


@pytest.mark.asyncio
async def test_preflight_scrubs_before_transient_backoff(monkeypatch):
    """An x-access-token URL that then hits a transient DNS failure is scrubbed in
    the DB first — the token is gone even while the row merely backs off."""
    def fake_validate(data, *, settings, resolve, resolver=None):
        raise ExternalGitTransientError("host resolution timed out")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo()
    claim = _preflight_claim(
        remote_url="https://x-access-token:ghp_leak@github.com/o/r.git"  # pragma: allowlist secret
    )
    rc = await poller._run_preflight(claim, repo)

    assert rc == 0
    assert repo.calls[0] == (
        "scrub", "v-1", "https://github.com/o/r.git", "ghp_leak",
        "https://x-access-token:ghp_leak@github.com/o/r.git", None,  # pragma: allowlist secret
    )
    assert repo.calls[1][0] == "mark_failure"
    assert repo.calls[1][5] == "pending_preflight"  # backoff stays pending
    # ...and carries the validated (scrubbed_url, migrated token) snapshot guard.
    assert repo.calls[1][6:] == ("https://github.com/o/r.git", "ghp_leak")


# ── Snapshot-CAS orchestration — a stale validation result never
#    clobbers, nor terminalizes, a config an operator reconfigured mid-flight ──
@pytest.mark.asyncio
async def test_preflight_scrub_zero_row_reloads_and_activates_clean_url(monkeypatch):
    """The pre-DNS scrub's old-value CAS matches 0 rows (the operator reconfigured
    to a clean URL under us). The pass does NOT proceed with the stale claimed
    value: it reloads the CURRENT row and re-classifies it from scratch — here the
    reloaded URL is already credential-free, so no re-scrub is needed and it
    activates the CURRENT config under its OWN snapshot guard."""
    def fake_validate(data, *, settings, resolve, resolver=None):
        return types.SimpleNamespace(canonical_url="https://github.com/o/r.git")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo(
        scrub_results=[False],  # claimed old-value scrub: 0 rows (config changed)
        rows={"v-1": _reload_row(remote_url="https://github.com/o/r.git", auth_token=None)},
    )
    claim = _preflight_claim(
        remote_url="https://x-access-token:ghp_old@github.com/o/r.git"  # pragma: allowlist secret
    )
    rc = await poller._run_preflight(claim, repo)

    assert rc == 1
    # scrub (0-row) → reload → NO re-scrub (reloaded URL already clean) → activate
    # the CURRENT config, guarded on its own (clean url, None) snapshot.
    assert repo.calls == [
        ("scrub", "v-1", "https://github.com/o/r.git", "ghp_old",
         "https://x-access-token:ghp_old@github.com/o/r.git", None),  # pragma: allowlist secret
        ("get", "v-1"),
        ("activate", "v-1", "https://github.com/o/r.git", None, 300,
         "https://github.com/o/r.git", None),
    ]


@pytest.mark.asyncio
async def test_preflight_scrub_zero_row_reloads_and_rescrubs_new_credential(monkeypatch):
    """The claimed scrub matches 0 rows, and the reloaded CURRENT remote_url embeds
    a DIFFERENT credential (operator reconfigured to another x-access-token URL).
    The pass re-classifies the CURRENT value: scrubs IT (old-value CAS on the
    current url/token) and activates the current config — never the stale one."""
    def fake_validate(data, *, settings, resolve, resolver=None):
        return types.SimpleNamespace(canonical_url="https://github.com/o/r.git")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo(
        scrub_results=[False, True],  # claimed scrub 0-row, current re-scrub matches
        rows={"v-1": _reload_row(
            remote_url="https://x-access-token:ghp_new@github.com/o/r.git",  # pragma: allowlist secret
            auth_token=None,
        )},
    )
    claim = _preflight_claim(
        remote_url="https://x-access-token:ghp_old@github.com/o/r.git"  # pragma: allowlist secret
    )
    rc = await poller._run_preflight(claim, repo)

    assert rc == 1
    scrubs = [c for c in repo.calls if c[0] == "scrub"]
    assert len(scrubs) == 2
    # First scrub CAS is guarded on the CLAIMED old values.
    assert scrubs[0] == (
        "scrub", "v-1", "https://github.com/o/r.git", "ghp_old",
        "https://x-access-token:ghp_old@github.com/o/r.git", None,  # pragma: allowlist secret
    )
    # The reload re-scrub targets the CURRENT dirty value, migrating the current
    # URL token, guarded on the CURRENT (url, token).
    assert scrubs[1] == (
        "scrub", "v-1", "https://github.com/o/r.git", "ghp_new",
        "https://x-access-token:ghp_new@github.com/o/r.git", None,  # pragma: allowlist secret
    )
    # ...and it activates the CURRENT config under its own snapshot guard.
    assert repo.calls[-1] == (
        "activate", "v-1", "https://github.com/o/r.git", "ghp_new", 300,
        "https://github.com/o/r.git", "ghp_new",
    )


@pytest.mark.asyncio
async def test_preflight_activation_snapshot_miss_reloads_superseded(monkeypatch):
    """activate's snapshot-CAS matches 0 rows because an operator QUARANTINED the
    row while preflight resolved DNS. The pass reloads, sees a non-pending state,
    and ends with no state change (0) — it never resurrects a quarantined row."""
    def fake_validate(data, *, settings, resolve, resolver=None):
        return types.SimpleNamespace(canonical_url="https://github.com/o/r.git")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo(
        activate_ok=False,  # snapshot-CAS 0 rows
        rows={"v-1": _reload_row(sync_state="quarantined")},
    )
    rc = await poller._run_preflight(_preflight_claim(), repo)

    assert rc == 0  # superseded → nothing-done, NOT a spurious success
    assert repo.calls == [
        ("activate", "v-1", "https://github.com/o/r.git", None, 300,
         "https://github.com/o/r.git", None),
        ("get", "v-1"),
    ]


@pytest.mark.asyncio
async def test_preflight_activation_snapshot_miss_reloads_and_converges(monkeypatch):
    """activate's snapshot-CAS matches 0 rows because the operator RECONFIGURED the
    remote (still pending) mid-DNS. The pass reloads the CURRENT config and
    activates THAT under its own snapshot guard — the stale validated URL is never
    written."""
    def fake_validate(data, *, settings, resolve, resolver=None):
        # canonical == the input url (identity) so we can see WHICH url is activated.
        return types.SimpleNamespace(canonical_url=data["remote_url"])

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo(
        activate_results=[False, True],  # first (stale) activate 0-row, second matches
        rows={"v-1": _reload_row(remote_url="https://github.com/o/r2.git")},
    )
    rc = await poller._run_preflight(_preflight_claim(), repo)  # claim url is r.git

    assert rc == 1
    activates = [c for c in repo.calls if c[0] == "activate"]
    assert len(activates) == 2
    # First activate snapshot-guards the STALE claimed config (r.git) → 0 rows.
    assert activates[0][5:] == ("https://github.com/o/r.git", None)
    # After reload it activates the CURRENT config (r2.git) under its OWN snapshot.
    assert activates[1][1:] == (
        "v-1", "https://github.com/o/r2.git", None, 300,
        "https://github.com/o/r2.git", None,
    )


@pytest.mark.asyncio
async def test_preflight_does_not_converge_under_persistent_race(monkeypatch):
    """A persistent racer keeps every activate snapshot-CAS at 0 rows (the config
    changes under each attempt). The pass is bounded by ``_PREFLIGHT_ATTEMPTS`` and
    ends with NO stale transition (rc 0) — never a stale activate / quarantine."""
    def fake_validate(data, *, settings, resolve, resolver=None):
        return types.SimpleNamespace(canonical_url="https://github.com/o/r.git")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo(
        activate_ok=False,  # every activate misses
        rows={"v-1": _reload_row()},  # always reloads a still-pending row
    )
    rc = await poller._run_preflight(_preflight_claim(), repo)

    assert rc == 0  # bounded, no state change
    activates = [c for c in repo.calls if c[0] == "activate"]
    gets = [c for c in repo.calls if c[0] == "get"]
    assert len(activates) == poller._PREFLIGHT_ATTEMPTS  # bounded attempts
    assert len(gets) == poller._PREFLIGHT_ATTEMPTS         # one reload per miss
    # A persistent race NEVER produces a quarantine (a stale terminalization).
    assert not any(c[0] == "quarantine" for c in repo.calls)


# ── Malformed URL is redacted + quarantined ATOMICALLY, and never
#    strands a credential in the terminal row ──────────────────────────────
@pytest.mark.asyncio
async def test_preflight_malformed_url_redacts_and_quarantines_atomically(monkeypatch):
    """A URL urlsplit cannot parse is redacted to the sentinel AND quarantined in
    ONE atomic repo call — no separate scrub+quarantine window.
    There is no validate and no transient backoff loop."""
    def boom(*a, **k):
        raise AssertionError("validate must not run for a malformed URL")

    monkeypatch.setattr(poller, "validate", boom)
    repo = FakeRepo()
    claim = _preflight_claim(remote_url="https://[::1")  # unbalanced IPv6 bracket
    rc = await poller._run_preflight(claim, repo)

    assert rc == 1
    # A single atomic redact+quarantine guarded on the claimed old (url, token).
    assert repo.calls == [
        ("redact_mq", "v-1", "https://[::1", None, poller._QUARANTINE_MALFORMED),
    ]


@pytest.mark.asyncio
async def test_preflight_malformed_url_with_credential_is_redacted(monkeypatch):
    """A malformed URL whose (unparseable) authority embeds a credential is passed
    as the CAS guard (claimed_url) to the atomic redact+quarantine; the repo writes
    a fixed sentinel + NULL token, so the terminal row carries no secret. The fixed
    reason code is itself credential-free."""
    def boom(*a, **k):
        raise AssertionError("validate must not run for a malformed URL")

    monkeypatch.setattr(poller, "validate", boom)
    repo = FakeRepo()
    # urlsplit raises on this (bad IPv6 literal) even though it carries a token.
    claim = _preflight_claim(remote_url="https://x-access-token:ghp_leak@[::1")  # pragma: allowlist secret
    rc = await poller._run_preflight(claim, repo)

    assert rc == 1
    call = repo.calls[0]
    assert call[0] == "redact_mq"
    # The claimed_url (CAS guard) is the raw value; the sentinel the repo WRITES is
    # asserted in the repo unit test. The reason code is fixed + secret-free.
    assert call[1:] == (
        "v-1", "https://x-access-token:ghp_leak@[::1", None,  # pragma: allowlist secret
        poller._QUARANTINE_MALFORMED,
    )
    assert "ghp_leak" not in poller._QUARANTINE_MALFORMED


@pytest.mark.asyncio
async def test_preflight_malformed_hygiene_scrubs_already_quarantined_row(monkeypatch):
    """The atomic redact+quarantine matches 0 rows because a peer already
    quarantined this EXACT malformed config (for another reason). The pass
    hygiene-scrubs the stranded credential off the quarantined row (preserving the
    first reason) and does not count it as new work (rc 0)."""
    def boom(*a, **k):
        raise AssertionError("validate must not run for a malformed URL")

    monkeypatch.setattr(poller, "validate", boom)
    repo = FakeRepo(redact_mq_ok=False, redact_hygiene_ok=True)
    claim = _preflight_claim(remote_url="https://x-access-token:ghp_leak@[::1")  # pragma: allowlist secret
    rc = await poller._run_preflight(claim, repo)

    assert rc == 0  # hygiene-only, not a fresh quarantine
    assert repo.calls == [
        ("redact_mq", "v-1", "https://x-access-token:ghp_leak@[::1", None,  # pragma: allowlist secret
         poller._QUARANTINE_MALFORMED),
        ("redact_hygiene", "v-1", "https://x-access-token:ghp_leak@[::1", None),  # pragma: allowlist secret
    ]


@pytest.mark.asyncio
async def test_preflight_malformed_zero_row_reloads_and_reclassifies(monkeypatch):
    """The atomic redact+quarantine AND the hygiene scrub both match 0 rows (the
    operator reconfigured the malformed URL to a valid one under us). The pass
    reloads the CURRENT row and re-classifies it from scratch — here it is now a
    valid clean URL, so it validates + activates (0-row →
    reload, never a stale quarantine)."""
    def fake_validate(data, *, settings, resolve, resolver=None):
        return types.SimpleNamespace(canonical_url="https://github.com/o/r.git")

    monkeypatch.setattr(poller, "validate", fake_validate)
    repo = FakeRepo(
        redact_mq_ok=False, redact_hygiene_ok=False,
        rows={"v-1": _reload_row(remote_url="https://github.com/o/r.git")},
    )
    claim = _preflight_claim(remote_url="https://[::1")  # malformed at claim time
    rc = await poller._run_preflight(claim, repo)

    assert rc == 1
    kinds = [c[0] for c in repo.calls]
    assert kinds == ["redact_mq", "redact_hygiene", "get", "activate"]
    # It activated the reloaded CLEAN config, never terminalized the stale malformed one.
    assert repo.calls[-1][:2] == ("activate", "v-1")
    assert repo.calls[-1][5:] == ("https://github.com/o/r.git", None)


# ── A superseded quarantine (already-terminal row) is not counted ──
@pytest.mark.asyncio
async def test_preflight_quarantine_superseded_is_not_counted(monkeypatch):
    """A quarantine whose snapshot-CAS matched 0 rows because the row was ALREADY
    moved to a terminal state by an operator/peer: the pass reloads, sees a
    non-pending state, and ends with rc 0 — NOT counted as processed work, state
    left untouched."""
    def boom(*a, **k):
        raise AssertionError("validate must not run for an unscrubbable URL")

    monkeypatch.setattr(poller, "validate", boom)
    # Unscrubbable (user:pass) → the quarantine branch; quarantine returns 0 rows,
    # and the reload shows the row is already quarantined.
    repo = FakeRepo(quarantine_ok=False, rows={"v-1": _reload_row(sync_state="quarantined")})
    claim = _preflight_claim(remote_url="https://user:pass@github.com/o/r.git")  # pragma: allowlist secret
    rc = await poller._run_preflight(claim, repo)

    assert rc == 0  # superseded quarantine is not a processed-work count
    assert repo.calls == [
        ("scrub", "v-1", "https://github.com/o/r.git", None,
         "https://user:pass@github.com/o/r.git", None),  # pragma: allowlist secret
        ("quarantine", "v-1", poller._QUARANTINE_LEGACY_CREDENTIAL, "pending_preflight",
         "https://github.com/o/r.git", None),
        ("get", "v-1"),
    ]


# ── reconcile failure branching ──────────────────────────────────────
def _reconcile_claim() -> dict:
    return {"vault_id": "v-9", "vault_name": "mirror-9", "poll_retry_count": 1}


def _fake_service(exc: BaseException | None):
    async def reconcile(vault_id, vault_name):
        if exc is not None:
            raise exc
        return {"status": "synced"}

    return types.SimpleNamespace(reconcile=reconcile)


@pytest.mark.asyncio
async def test_reconcile_success(monkeypatch):
    monkeypatch.setattr(poller, "_get_service", lambda: _fake_service(None))
    repo = FakeRepo()
    rc = await poller._run_reconcile(_reconcile_claim(), repo)
    assert rc == 1
    assert repo.calls == []  # reconcile records its own success internally


@pytest.mark.asyncio
async def test_reconcile_policy_violation_quarantines(monkeypatch):
    monkeypatch.setattr(
        poller, "_get_service",
        lambda: _fake_service(ExternalGitPolicyError("exec-time re-validation failed")),
    )
    repo = FakeRepo()
    rc = await poller._run_reconcile(_reconcile_claim(), repo)
    assert rc == 0
    # The reconcile path quarantines expecting the exact 'active' state it claimed
    # the row in (fix-4), state-only (no old-value snapshot guard — the poller does
    # not carry the reconcile's fetched config), so the snapshot slots are None.
    assert repo.calls == [("quarantine", "v-9", poller._QUARANTINE_POLICY, "active", None, None)]


@pytest.mark.parametrize(
    "exc, expect_code",
    [
        (ExternalGitTransientError("dns"), "transient"),
        (RuntimeError("boom"), "internal_error"),
    ],
)
@pytest.mark.asyncio
async def test_reconcile_transient_backs_off(monkeypatch, exc, expect_code):
    monkeypatch.setattr(poller, "_get_service", lambda: _fake_service(exc))
    repo = FakeRepo()
    rc = await poller._run_reconcile(_reconcile_claim(), repo)
    assert rc == 0
    assert len(repo.calls) == 1
    kind, vault_id, code, _msg, backoff, expected, vurl, vtok = repo.calls[0]
    assert (kind, vault_id, code) == ("mark_failure", "v-9", expect_code)
    assert backoff == poller.next_attempt_delay(1)
    # A reconcile backoff stays 'active' (its expected-state CAS) and is state-only
    # on the poller side (no snapshot guard — those slots are None).
    assert expected == "active"
    assert (vurl, vtok) == (None, None)


# ── A superseded reconcile / quarantine is not counted as work ─
@pytest.mark.asyncio
async def test_reconcile_superseded_status_is_not_counted(monkeypatch):
    """``reconcile`` returning ``status='superseded'`` (its own mark_success CAS
    matched 0 rows — the row was quarantined mid-reconcile) is logged but NOT
    counted as processed work, and the poller touches no state."""
    async def reconcile(vault_id, vault_name):
        return {"status": "superseded", "sha": "abc123"}

    monkeypatch.setattr(
        poller, "_get_service", lambda: types.SimpleNamespace(reconcile=reconcile)
    )
    repo = FakeRepo()
    rc = await poller._run_reconcile(_reconcile_claim(), repo)
    assert rc == 0  # superseded → not a processed-work count
    assert repo.calls == []  # no quarantine / mark — reconcile owns its bookkeeping


@pytest.mark.asyncio
async def test_reconcile_quarantine_superseded_still_returns_zero(monkeypatch):
    """A reconcile-time policy violation whose quarantine CAS matched 0 rows (the
    row was already terminal) is a superseded no-op: still rc 0, state untouched,
    logged as superseded rather than as a fresh quarantine."""
    monkeypatch.setattr(
        poller, "_get_service",
        lambda: _fake_service(ExternalGitPolicyError("exec-time re-validation failed")),
    )
    repo = FakeRepo(quarantine_ok=False)
    rc = await poller._run_reconcile(_reconcile_claim(), repo)
    assert rc == 0
    # State-only on the reconcile path (snapshot slots None).
    assert repo.calls == [("quarantine", "v-9", poller._QUARANTINE_POLICY, "active", None, None)]


# ── ExternalGitService.reconcile() converts a 0-row mark_success into a
#    'superseded' status on BOTH the unchanged and the synced branch ──────────
@pytest.mark.parametrize(
    "action, mark_ok, expect_status, expect_rc",
    [
        ("unchanged", False, "superseded", 0),
        ("unchanged", True,  "unchanged",  1),
        ("fetched",   False, "superseded", 0),
        ("fetched",   True,  "synced",     1),
    ],
)
@pytest.mark.asyncio
async def test_reconcile_mark_success_branches(monkeypatch, action, mark_ok,
                                               expect_status, expect_rc):
    """The poller tests elsewhere mock the service returning 'superseded'
    directly; this drives the REAL ``ExternalGitService.reconcile()`` so its two
    ``mark_success`` → superseded conversions are covered. On BOTH the unchanged
    and the synced branch, a 0-row (snapshot-CAS missed — quarantined/reconfigured
    mid-reconcile) becomes ``status='superseded'`` and the poller counts it as 0;
    a matched write yields the normal status and counts 1. The snapshot-CAS
    carries the fetched (remote_url, auth_token)."""
    cfg = {
        "remote_url": "https://github.com/o/r.git",
        "remote_branch": "main",
        "auth_token": None,
        "poll_interval_secs": 300,
        "last_synced_sha": "old_sha" if action == "unchanged" else None,
    }
    ms_calls: list[dict] = []

    class _Repo:
        def __init__(self, pool):
            pass

        async def get(self, vault_id):
            return cfg

        async def mark_success(self, vault_id, poll_interval_secs, new_sha=None,
                               *, validated_url=None, validated_token=None):
            ms_calls.append(
                {"new_sha": new_sha, "validated_url": validated_url,
                 "validated_token": validated_token}
            )
            return mark_ok

    class _DocRepo:
        def __init__(self, pool):
            pass

        async def list_external_blobs(self, vault_id):
            return {}

    class _Git:
        def vault_exists(self, name):
            return False

        def ls_remote_head(self, url, branch, token):
            return "hint_sha"

        def ls_tree(self, name, sha):
            return {}  # empty tree → no per-file reindex, straight to mark_success

    async def _fake_pool():
        return object()

    monkeypatch.setattr(egs, "get_pool", _fake_pool)
    monkeypatch.setattr(egs, "VaultExternalGitRepository", _Repo)
    monkeypatch.setattr(egs, "DocumentRepository", _DocRepo)

    svc = egs.ExternalGitService(git=_Git())
    materialized = "mat_sha"
    svc.ensure_local_bare = lambda *a, **k: (action, materialized)

    # ── direct: reconcile() returns the right status per (branch, mark_ok) ──
    result = await svc.reconcile("v-svc", "svc-mirror")
    assert result["status"] == expect_status
    # The snapshot-CAS carried the fetched config on BOTH branches...
    assert ms_calls[-1]["validated_url"] == cfg["remote_url"]
    assert ms_calls[-1]["validated_token"] == cfg["auth_token"]
    # ...and only the synced branch advances the cursor (new_sha).
    if action == "unchanged":
        assert ms_calls[-1]["new_sha"] is None
    else:
        assert ms_calls[-1]["new_sha"] == materialized

    # ── end-to-end: the poller counts superseded as 0, a real status as 1 ──
    monkeypatch.setattr(poller, "_get_service", lambda: svc)
    rc = await poller._run_reconcile(_reconcile_claim(), FakeRepo())
    assert rc == expect_rc


# ── claim SQL only ever consults the hardened columns ────────────────
@pytest.mark.asyncio
async def test_claim_preflight_sql_filters_pending_preflight():
    conn = CaptureConn()
    await poller._claim_preflight(conn)
    sql = conn.sql
    assert "sync_state = 'pending_preflight'" in sql
    assert "poll_next_at <= NOW()" in sql
    # The legacy rollout-fence columns must NOT be consulted by the new poller.
    assert "next_attempt_at" not in sql
    assert sql.count("retry_count") == sql.count("poll_retry_count")


@pytest.mark.asyncio
async def test_claim_reconcile_sql_filters_active_only():
    conn = CaptureConn()
    await poller._claim_reconcile(conn)
    sql = conn.sql
    assert "sync_state = 'active'" in sql
    assert "poll_retry_count < $1" in sql
    assert "poll_next_at <= NOW()" in sql
    # Legacy fence columns are never referenced.
    assert "next_attempt_at" not in sql
    assert sql.count("retry_count") == sql.count("poll_retry_count")


# ── pending_stats shape ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pending_stats_reports_state_machine(monkeypatch):
    counts = {
        "total": 5, "pending_preflight": 1, "active": 3, "quarantined": 1,
        "due": 2, "retrying": 1, "abandoned": 0,
    }

    class _Conn:
        async def fetchrow(self, *_a, **_k):
            return counts

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_a):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    async def fake_get_pool():
        return _Pool()

    monkeypatch.setattr(poller, "get_pool", fake_get_pool)
    assert await poller.pending_stats() == counts


# ── Reconcile is not starved by a steady preflight influx ────
class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_a):
        return False


class _ProcConn:
    def transaction(self):
        return _Txn()


class _ProcAcq:
    async def __aenter__(self):
        return _ProcConn()

    async def __aexit__(self, *_a):
        return False


class _ProcPool:
    def acquire(self):
        return _ProcAcq()


def _patch_process_once(monkeypatch, *, preflight, reconcile):
    """Wire _process_once onto fakes, recording which processors run and in what
    order. ``preflight`` / ``reconcile`` are the claim dicts (or None)."""
    async def fake_get_pool():
        return _ProcPool()

    async def fake_claim_preflight(conn):
        return preflight

    async def fake_claim_reconcile(conn):
        return reconcile

    ran: list[str] = []

    async def fake_run_preflight(claim, repo):
        ran.append("preflight")
        return 1

    async def fake_run_reconcile(claim, repo):
        ran.append("reconcile")
        return 1

    monkeypatch.setattr(poller, "get_pool", fake_get_pool)
    monkeypatch.setattr(poller, "_claim_preflight", fake_claim_preflight)
    monkeypatch.setattr(poller, "_claim_reconcile", fake_claim_reconcile)
    monkeypatch.setattr(poller, "_run_preflight", fake_run_preflight)
    monkeypatch.setattr(poller, "_run_reconcile", fake_run_reconcile)
    return ran


@pytest.mark.asyncio
async def test_process_once_runs_both_preflight_and_reconcile(monkeypatch):
    """With BOTH a due preflight and a due active mirror, a single pass processes
    one of EACH — the reconcile is not skipped just because a preflight was also
    due, so a never-empty pending_preflight queue can't starve active reconcile."""
    ran = _patch_process_once(
        monkeypatch,
        preflight={"vault_id": "pf", "vault_name": "pf", "poll_retry_count": 0},
        reconcile={"vault_id": "rc", "vault_name": "rc", "poll_retry_count": 0},
    )
    did = await poller._process_once()
    assert ran == ["preflight", "reconcile"]
    assert did == 2


@pytest.mark.asyncio
async def test_process_once_reconciles_with_no_preflight(monkeypatch):
    """A due active mirror is reconciled even when no preflight is due."""
    ran = _patch_process_once(
        monkeypatch,
        preflight=None,
        reconcile={"vault_id": "rc", "vault_name": "rc", "poll_retry_count": 0},
    )
    did = await poller._process_once()
    assert ran == ["reconcile"]
    assert did == 1


@pytest.mark.asyncio
async def test_process_once_idle_is_zero(monkeypatch):
    """Nothing due on either path → no work, no crash."""
    ran = _patch_process_once(monkeypatch, preflight=None, reconcile=None)
    did = await poller._process_once()
    assert ran == []
    assert did == 0


# ── rollout fence: simulate the pre-hardening poller's claim predicate ─
_OLD_POLLER_MAX_RETRIES = 8


def _old_poller_would_claim(row, now, max_retries=_OLD_POLLER_MAX_RETRIES) -> bool:
    """Verbatim predicate of the migration-010 poller's ``_claim_one`` WHERE:
    ``next_attempt_at <= NOW() AND retry_count < MAX_RETRIES``. The whole point
    of the migration-049 fence is to make this evaluate False for every row."""
    return row["next_attempt_at"] <= now and row["retry_count"] < max_retries


def test_rollout_fence_blocks_old_poller():
    now = dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc)
    inf = dt.datetime.max.replace(tzinfo=dt.timezone.utc)  # TIMESTAMPTZ 'infinity'

    # BEFORE the fence a due legacy row WOULD be claimed (control).
    assert _old_poller_would_claim(
        {"next_attempt_at": now - dt.timedelta(hours=1), "retry_count": 0}, now
    ) is True

    # AFTER migration 049 the row is fenced to ('infinity', 8): claim is False.
    assert _old_poller_would_claim(
        {"next_attempt_at": inf, "retry_count": _OLD_POLLER_MAX_RETRIES}, now
    ) is False

    # Each fence predicate alone already suffices (belt-and-suspenders).
    assert _old_poller_would_claim({"next_attempt_at": inf, "retry_count": 0}, now) is False
    assert _old_poller_would_claim(
        {"next_attempt_at": now - dt.timedelta(hours=1),
         "retry_count": _OLD_POLLER_MAX_RETRIES}, now
    ) is False


def test_migration_049_source_encodes_fence_and_safe_check_order():
    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "db" / "migrations" / "049_external_git_quarantine.py"
    ).read_text(encoding="utf-8")
    # Existing rows fenced + future inserts fenced (DEFAULT flip).
    assert "next_attempt_at = 'infinity'" in src
    assert "ALTER COLUMN next_attempt_at SET DEFAULT 'infinity'" in src
    # The fence is DB-ENFORCED by a CHECK so an old binary
    # that claimed a row just before the migration cannot later unfence it.
    assert "vault_external_git_rollout_fence" in src
    assert "CHECK (next_attempt_at = 'infinity'::timestamptz" in src
    # New state machine + its claim scheduler columns.
    for col in ("sync_state", "poll_next_at", "poll_retry_count"):
        assert col in src
    # CHECKs added NOT VALID → VALIDATE so legacy data can't fail the apply.
    assert "NOT VALID" in src
    assert "VALIDATE CONSTRAINT" in src
