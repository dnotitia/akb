"""Background poller for external_git mirror vaults (quarantine machine).

Each mirror row moves through a small state machine:

    pending_preflight ──(Layer-2 re-validate + credential scrub OK)──▶ active
          │  ▲                                                           │
          │  └────────────(transient: DNS/network — backoff)────────────┘
          │                                                              │
          └──(permanent policy violation / unscrubbable cred)──▶ quarantined ◀──(permanent
                                                                                 policy violation
                                                                                 during reconcile)

Two claim paths, tried per iteration:

* **preflight** — claims a due ``pending_preflight`` row, re-runs the shared
  validator (``validate(row, resolve=True)`` — closes TOCTOU / stale resolution)
  and scrubs any legacy URL-embedded credential, then promotes it to ``active`` (or
  quarantines / backs off). Cheap: no network fetch, just DNS + a DB write.
* **reconcile** — claims a due ``active`` row and runs one
  ``ExternalGitService.reconcile`` (network + git fetch + per-file DB writes).

Only ``active`` rows are ever reconciled; ``quarantined`` rows are claimed by
neither path. The scheduler runs entirely on the hardened columns
``sync_state`` / ``poll_next_at`` / ``poll_retry_count`` (migration 049) — the
legacy ``next_attempt_at`` / ``retry_count`` are the rollout fence for the
pre-hardening binary and are neither read nor written here.

Loop mechanics (start/stop, idle cadence) come from ``_backfill``. Backoff on
transient failures uses the shared schedule (60s → 6h, capped at
``MAX_RETRIES`` before an active row stops being claimed / a preflight keeps
retrying). Poller start is gated on ``external_git_enabled`` in ``lifecycle``.

Each iteration claims a single vault. Reconcile is heavy (network + git fetch +
per-file DB writes) so batching many vaults into one iteration would waste the
SKIP LOCKED window for nothing — the loop simply spins again immediately when
work remains.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

from app.db.postgres import get_pool
from app.repositories.vault_external_git_repo import VaultExternalGitRepository
from app.services._backfill import BackfillRunner, MAX_RETRIES, next_attempt_delay
from app.services.external_git_runner import ExternalGitCommandError
from app.services.external_git_service import ExternalGitService
from app.services.external_git_validation import (
    ExternalGitPolicyError,
    ExternalGitTransientError,
    validate,
)

logger = logging.getLogger("akb.external_git_poller")

# Fixed, secret-free quarantine reason codes: value-less enums,
# never a URL / token / raw exception string. Persisted in
# ``vault_external_git.sync_state_reason``.
_QUARANTINE_POLICY = "policy_violation"
_QUARANTINE_LEGACY_CREDENTIAL = "legacy_credential"
_QUARANTINE_MALFORMED = "malformed_url"

# Bounded preflight attempts under a config race. Every DB write in a
# preflight pass is a snapshot-CAS that fires ONLY
# against the exact stored config this pass read; a 0-row result means an operator
# reconfigured the remote under the in-flight preflight, so we RELOAD the current
# row and re-classify it from scratch — never proceeding with, or terminalizing,
# the stale claimed value. The cap stops a persistent racer from spinning here
# forever: on exhaustion the pass ends with no state change (0), leaving the row
# pending_preflight for its next due tick.
_PREFLIGHT_ATTEMPTS = 3


# Lazily-constructed shared service (MINOR). Building it eagerly at *import*
# also builds ``GitService()``, whose ``__init__`` mkdir's
# ``git_storage_path``/``_worktrees`` — an import-time filesystem side effect
# that fires even on a disabled or read-only deployment, breaks tool/CLI imports
# that never poll, and forces every test that imports this module to redirect
# storage first. Deferring construction to the first ``_process_once`` keeps the
# module import pure while preserving the process-wide single-instance contract:
# the poller loop is a single asyncio task, so the unguarded check-and-set below
# (no ``await`` between test and assignment) cannot interleave.
_service: ExternalGitService | None = None


def _get_service() -> ExternalGitService:
    """Return the shared :class:`ExternalGitService`, building it on first use."""
    global _service
    svc = _service
    if svc is None:
        svc = ExternalGitService()
        _service = svc
    return svc


def _classify_failure(exc: BaseException) -> tuple[str, str]:
    """Map a TRANSIENT reconcile/preflight exception to a FIXED (code, static
    safe_message) at the outer boundary. The raw exception
    never reaches the DB or the log verbatim — an arbitrary exception may carry
    an un-sanitized remote URL or git stderr.

    Permanent ``ExternalGitPolicyError`` is handled separately (quarantine) and
    does not flow through here; its arm is retained for defensive completeness.
    """
    if isinstance(exc, ExternalGitTransientError):
        return (
            "transient",
            "external git host resolution or network was temporarily unavailable",
        )
    if isinstance(exc, ExternalGitPolicyError):
        return ("policy_violation", "external git remote failed a security policy check")
    if isinstance(exc, ExternalGitCommandError):
        return ("git_command_failed", "external git command failed")
    return ("internal_error", "external git sync failed")


def _safe_detail(exc: BaseException) -> str:
    """A log-safe rendering of ``exc``: its message ONLY for types that guarantee
    a secret-free message by construction (the command error is pre-sanitized;
    the policy/transient errors name the violation class only). Any other
    exception yields no detail — ``str(e)`` is never logged for it."""
    if isinstance(
        exc, (ExternalGitCommandError, ExternalGitPolicyError, ExternalGitTransientError)
    ):
        return str(exc)
    return ""


# ── Legacy credential scrub ──────────────────────────────


def _scrub_userinfo(raw_url: str) -> tuple[str, str | None, bool]:
    """Split any embedded userinfo out of a legacy ``remote_url``.

    Returns ``(url_without_userinfo, migrated_token_or_None, migratable)``:

    * No userinfo → ``(raw_url, None, True)`` — nothing to scrub; the URL is
      passed on to the validator unchanged (which canonicalizes it).
    * ``x-access-token:<token>@host…`` → ``(host_url, <token>, True)`` — the one
      supported credential form (GitHub/GitLab PAT-in-URL). The token is lifted
      out to become the ``auth_token`` column value.
    * ANY other userinfo form (bare ``token@``, ``user:pass@``, ``oauth2:…@``,
      doubled userinfo, …) → ``(host_url, None, False)`` — the credential cannot
      be safely migrated to ``auth_token`` so the caller quarantines, but the
      returned URL is still CREDENTIAL-FREE (userinfo stripped) so the caller can
      persist it and leave no secret behind in ``remote_url``.

    A malformed URL that ``urlsplit`` cannot parse (e.g. a bad IPv6 literal) is a
    PERMANENT syntax violation, not a transient failure — raise a secret-free
    ``ExternalGitPolicyError`` so the caller QUARANTINES it rather than backing
    off forever. The message is a FIXED string (never
    ``str(exc)`` or the URL), and the ``ValueError`` cause is suppressed
    (``from None``), so no unvalidated input can reach a log.

    Only userinfo is removed; scheme/host/port/path/query/fragment are preserved
    verbatim so the downstream validator still rejects (and thereby quarantines)
    a structurally invalid legacy URL — e.g. one carrying a query string — rather
    than this helper silently "cleaning" it. The token is passed through opaque
    (not percent-decoded); the validator re-guards its charset/length.
    """
    try:
        parts = urlsplit(raw_url)
    except ValueError:
        raise ExternalGitPolicyError(
            "external_git remote_url is malformed and cannot be parsed"
        ) from None
    netloc = parts.netloc
    if "@" not in netloc:
        return raw_url, None, True
    userinfo, _, hostpart = netloc.rpartition("@")
    scrubbed = urlunsplit(
        (parts.scheme, hostpart, parts.path, parts.query, parts.fragment)
    )
    username, sep, password = userinfo.partition(":")
    if username == "x-access-token" and sep == ":" and password:
        return scrubbed, password, True
    return scrubbed, None, False


def _validate_scrubbed(
    scrubbed_url: str, remote_branch: str, effective_token: str | None, settings
) -> str:
    """DNS + policy re-validation of an ALREADY-scrubbed URL (runs off the event
    loop — ``validate(resolve=True)`` resolves DNS, closing TOCTOU / stale
    resolution). Returns the canonical URL. Raises ``ExternalGitPolicyError`` (→ quarantine,
    policy reason) or ``ExternalGitTransientError`` (→ backoff, stays
    pending_preflight).

    The userinfo split + credential scrub now happen in the async caller
    (``_run_preflight``) so the credential-free URL can be persisted to the DB
    BEFORE this network work; this helper is the pure DNS
    tail only.
    """
    # NOTE: poll_interval_secs is deliberately omitted from the validation dict.
    # It is not a remote-safety property (the DB CHECK already pins it to the
    # hard bounds), so re-checking it here would quarantine a perfectly safe
    # mirror merely because an operator RAISED external_git_poll_interval_min.
    validated = validate(
        {
            "remote_url": scrubbed_url,
            "remote_branch": remote_branch,
            "auth_token": effective_token,
        },
        settings=settings,
        resolve=True,
    )
    return validated.canonical_url


# ── Claim (two paths) ─────────────────────────────────────────────────


async def _claim_preflight(conn) -> dict | None:
    """Claim one due ``pending_preflight`` mirror. Advances ``poll_next_at`` by
    ``external_git_claim_lookahead_secs`` so a peer worker (or the next pass)
    skips this row while preflight is in flight; a successful activate resets
    ``poll_next_at`` to NOW(). Legacy fence columns are not consulted."""
    from app.config import settings  # local import to dodge circular import
    row = await conn.fetchrow(
        """
        WITH due AS (
            SELECT veg.vault_id, v.name AS vault_name
              FROM vault_external_git veg
              JOIN vaults v ON v.id = veg.vault_id
             WHERE veg.sync_state = 'pending_preflight'
               AND veg.poll_next_at <= NOW()
             ORDER BY veg.poll_next_at
             LIMIT 1
             FOR UPDATE OF veg SKIP LOCKED
        )
        UPDATE vault_external_git veg
           SET poll_next_at = NOW() + ($1 || ' seconds')::interval
          FROM due
         WHERE veg.vault_id = due.vault_id
        RETURNING veg.vault_id, due.vault_name, veg.remote_url,
                  veg.remote_branch, veg.auth_token, veg.poll_interval_secs,
                  veg.poll_retry_count
        """,
        str(settings.external_git_claim_lookahead_secs),
    )
    return dict(row) if row else None


async def _claim_reconcile(conn) -> dict | None:
    """Claim the most-due ``active`` mirror for a reconcile pass. Pushes
    ``poll_next_at`` forward by ``external_git_claim_lookahead_secs`` so peer
    workers (or the same worker on its next pass) skip this row while reconcile
    is in flight — the interval has to exceed the longest realistic initial
    bootstrap (a 1GB mirror clone can easily run past the default 10m).

    Only the hardened schedule columns are used; the legacy
    ``next_attempt_at`` / ``retry_count`` (the rollout fence for the
    pre-hardening poller) are intentionally NOT consulted."""
    from app.config import settings  # local import to dodge circular import
    row = await conn.fetchrow(
        """
        WITH due AS (
            SELECT veg.vault_id, v.name AS vault_name
              FROM vault_external_git veg
              JOIN vaults v ON v.id = veg.vault_id
             WHERE veg.sync_state = 'active'
               AND veg.poll_next_at <= NOW()
               AND veg.poll_retry_count < $1
             ORDER BY veg.poll_next_at
             LIMIT 1
             FOR UPDATE OF veg SKIP LOCKED
        )
        UPDATE vault_external_git veg
           SET poll_next_at = NOW() + ($2 || ' seconds')::interval
          FROM due
         WHERE veg.vault_id = due.vault_id
        RETURNING veg.vault_id, due.vault_name, veg.poll_retry_count
        """,
        MAX_RETRIES, str(settings.external_git_claim_lookahead_secs),
    )
    return dict(row) if row else None


# ── Malformed-URL redact + quarantine (atomic) ──────


async def _redact_malformed_and_quarantine(
    ext_repo: VaultExternalGitRepository,
    vault_id,
    vault_name: str,
    claimed_url: str,
    claimed_token: str | None,
    exc: ExternalGitPolicyError,
) -> int | None:
    """Redact an unparseable ``remote_url`` to a credential-free sentinel AND
    quarantine it ATOMICALLY. Returns 1 on a fresh quarantine,
    0 when the credential was hygiene-scrubbed off an already-quarantined row (no
    new work), or ``None`` when the stored config changed under us — the caller
    reloads + re-classifies the CURRENT config, never terminalizing a stale one.

    A URL ``urlsplit`` cannot parse is a PERMANENT syntax violation whose userinfo
    cannot be reliably stripped, so it must never be quarantined verbatim (that
    would strand any embedded credential in the terminal row). The single atomic
    repo UPDATE sets ``remote_url`` to a fixed value-less sentinel and
    ``auth_token`` to NULL as part of the SAME transition, guarded on the claimed
    old ``(remote_url, auth_token)`` + the exact 'pending_preflight' state (the
    only state a malformed URL is ever classified from) — so no operator
    reconfigure can slip a new credential-bearing URL between a separate redact
    and quarantine (the two-write window an earlier revision left open).
    """
    if await ext_repo.redact_malformed_and_quarantine(
        vault_id, claimed_url, claimed_token, _QUARANTINE_MALFORMED
    ):
        logger.warning(
            "External preflight quarantined (malformed URL): "
            "vault=%s reason=%s type=%s detail=%s",
            vault_name, _QUARANTINE_MALFORMED, type(exc).__name__, _safe_detail(exc),
        )
        return 1
    # 0 rows. Either a peer already quarantined this EXACT config (but may have
    # left the raw credential in remote_url) — hygiene-scrub it while PRESERVING
    # the first quarantine reason — or an operator reconfigured the remote (the
    # claimed old value is gone), which we treat as a config race and reload.
    if await ext_repo.redact_malformed_url_if_quarantined(
        vault_id, claimed_url, claimed_token
    ):
        logger.info(
            "External preflight redacted a stranded malformed credential on an "
            "already-quarantined row: vault=%s", vault_name,
        )
        return 0
    return None  # stored config changed under us → caller reloads + re-classifies


# ── Processors ─────────────────────────────────────────────────────────


async def _run_preflight(claim: dict, ext_repo: VaultExternalGitRepository) -> int:
    """Re-validate + scrub one claimed pending_preflight row, then activate /
    quarantine / back off. Returns 1 when the row's state advanced
    (activate / quarantine), 0 on a transient backoff, a SUPERSEDED transition, or
    a config race that did not converge — none of which is counted as processed
    work.

    Optimistic-concurrency orchestration: EVERY DB write a
    pass makes is a snapshot-CAS against the EXACT stored config it read, so a
    stale validation result can never be applied to — nor terminalize — a config
    an operator RECONFIGURED mid-flight. A snapshot-CAS that matches 0 rows means
    the stored ``(remote_url, auth_token)`` changed under us; we RELOAD the current
    row and re-classify it FROM SCRATCH (never proceeding with the stale claimed
    value), bounded by ``_PREFLIGHT_ATTEMPTS`` so a persistent racer cannot spin
    forever — on exhaustion the pass ends with no state change (the row stays
    pending_preflight for its next due tick).
    """
    vault_id = claim["vault_id"]
    vault_name = claim["vault_name"]
    # Working config: starts from the claim, reloaded from the CURRENT row on any
    # snapshot-CAS miss. Every transition below is guarded on exactly these values.
    config = {
        "remote_url": claim["remote_url"],
        "auth_token": claim.get("auth_token"),
        "remote_branch": claim["remote_branch"],
        "poll_interval_secs": claim["poll_interval_secs"],
        "poll_retry_count": claim["poll_retry_count"],
    }
    for _attempt in range(_PREFLIGHT_ATTEMPTS):
        outcome = await _preflight_attempt(ext_repo, vault_id, vault_name, config)
        if outcome is not None:
            return outcome
        # A snapshot-CAS matched 0 rows: the stored (remote_url, auth_token) changed
        # under us. Reload the CURRENT row and retry against it — never the stale
        # claimed value.
        current = await ext_repo.get(vault_id)
        if current is None:
            return 0  # row deleted under us — nothing to do
        if current["sync_state"] != "pending_preflight":
            # An operator/peer already moved it to a terminal (quarantined) or
            # active state: nothing left for preflight, not a stale transition.
            return 0
        config = {
            "remote_url": current["remote_url"],
            "auth_token": current.get("auth_token"),
            "remote_branch": current["remote_branch"],
            "poll_interval_secs": current["poll_interval_secs"],
            "poll_retry_count": current["poll_retry_count"],
        }
    logger.info(
        "External preflight did not converge under a persistent config race: "
        "vault=%s", vault_name,
    )
    return 0


async def _preflight_attempt(
    ext_repo: VaultExternalGitRepository,
    vault_id,
    vault_name: str,
    config: dict,
) -> int | None:
    """One optimistic-concurrency preflight attempt against a SPECIFIC stored
    config. Returns 0/1 when the pass reached a decision
    for this config (backoff / quarantine / activate / hygiene), or ``None`` when a
    snapshot-CAS matched 0 rows because the stored ``(remote_url, auth_token)``
    changed under us — the caller reloads the CURRENT row and retries, so a stale
    value is never validated, activated, or terminalized.

    Every terminal write carries the snapshot old-value ``(scrubbed_url,
    effective_token)`` — the exact config this attempt scrubbed + validated — so
    every terminal row is left credential-free and no stale result can clobber a
    newer operator config.
    """
    from app.config import settings  # local import to dodge circular import
    url = config["remote_url"]
    token = config["auth_token"]

    # 1. Split embedded userinfo (pure). A malformed URL cannot be split, so it is
    #    redacted to a sentinel + quarantined ATOMICALLY; a
    #    concurrent reconfigure there returns None → reload + re-classify.
    try:
        scrubbed_url, migrated_token, migratable = _scrub_userinfo(url)
    except ExternalGitPolicyError as e:
        return await _redact_malformed_and_quarantine(
            ext_repo, vault_id, vault_name, url, token, e
        )

    # The column token is authoritative once set; only adopt a URL-embedded token
    # when the column is empty (avoid clobbering a rotated column credential with a
    # stale in-URL one). After step 2 the stored row holds exactly (scrubbed_url,
    # effective_token) — the snapshot every transition below is guarded on.
    effective_token = token if token else migrated_token

    # 2. If the stored URL embedded userinfo, persist the credential-free URL +
    #    migrated token NOW — before DNS — via the OLD-VALUE CAS. A 0-row result
    #    means the stored config changed under us: bail to a reload (never proceed
    #    with the stale value).
    if scrubbed_url != url:
        if not await ext_repo.scrub_legacy_credential(
            vault_id, scrubbed_url, effective_token, url, token
        ):
            return None

    # 3. An unscrubbable credential form (anything but x-access-token:TOKEN) can't
    #    be migrated to auth_token → quarantine for manual re-auth. The URL is
    #    already credential-free from step 2.
    if not migratable:
        return await _quarantine_snapshot(
            ext_repo, vault_id, vault_name, _QUARANTINE_LEGACY_CREDENTIAL,
            scrubbed_url, effective_token,
        )

    # 4. DNS + policy re-validation of the scrubbed URL (off the event loop).
    try:
        canonical_url = await asyncio.to_thread(
            _validate_scrubbed,
            scrubbed_url, config["remote_branch"], effective_token, settings,
        )
    except ExternalGitPolicyError as e:
        return await _quarantine_snapshot(
            ext_repo, vault_id, vault_name, _QUARANTINE_POLICY,
            scrubbed_url, effective_token, exc=e,
        )
    except Exception as e:  # noqa: BLE001 — transient (DNS) or unexpected → backoff
        code, safe_message = _classify_failure(e)
        delay = next_attempt_delay(config["poll_retry_count"])
        logger.warning(
            "External preflight failed: vault=%s retry=%d backoff=%ds code=%s type=%s detail=%s",
            vault_name, config["poll_retry_count"], delay, code,
            type(e).__name__, _safe_detail(e),
        )
        marked = await ext_repo.mark_failure(
            vault_id, code, safe_message, delay, "pending_preflight",
            validated_url=scrubbed_url, validated_token=effective_token,
        )
        return 0 if marked else None

    # 5. Promote to active — snapshot-CAS on sync_state='pending_preflight' AND the
    #    validated (scrubbed_url, effective_token). A 0-row result means the row was
    #    quarantined OR reconfigured out from under us: reload + re-classify — never
    #    resurrect a quarantined row nor clobber a new config.
    activated = await ext_repo.activate_from_preflight(
        vault_id, canonical_url, effective_token, config["poll_interval_secs"],
        validated_url=scrubbed_url, validated_token=effective_token,
    )
    if not activated:
        return None
    logger.info("External preflight activated: vault=%s", vault_name)
    return 1


async def _quarantine_snapshot(
    ext_repo: VaultExternalGitRepository,
    vault_id,
    vault_name: str,
    reason_code: str,
    validated_url: str,
    validated_token: str | None,
    *,
    exc: ExternalGitPolicyError | None = None,
) -> int | None:
    """Quarantine a preflight row on a PERMANENT policy violation via a
    snapshot-CAS on the validated ``(remote_url, auth_token)`` + the exact
    'pending_preflight' state (the state the preflight claimed the row in).
    Returns 1 on a fresh quarantine, or ``None`` when the CAS matched 0 rows (the
    row was reconfigured, or already moved to a terminal/active state, under us) so
    the caller reloads + re-classifies — never terminalizing a stale config.
    The scrubbed URL was already persisted, so the terminal
    row is credential-free regardless.
    """
    if not await ext_repo.quarantine(
        vault_id, reason_code, "pending_preflight",
        validated_url=validated_url, validated_token=validated_token,
    ):
        return None
    if exc is not None:
        logger.warning(
            "External preflight quarantined: vault=%s reason=%s type=%s detail=%s",
            vault_name, reason_code, type(exc).__name__, _safe_detail(exc),
        )
    else:
        logger.warning(
            "External preflight quarantined: vault=%s reason=%s",
            vault_name, reason_code,
        )
    return 1


async def _run_reconcile(claim: dict, ext_repo: VaultExternalGitRepository) -> int:
    """Reconcile one claimed active row. Returns 1 on success, 0 otherwise
    (quarantine, transient backoff, or a SUPERSEDED transition — see below).

    A PERMANENT policy violation surfacing from the hermetic runner quarantines
    the row; any other failure is transient → backoff (the row stays
    active). ``reconcile`` records its own success via ``mark_success``; if that
    CAS was superseded (an operator/peer quarantined the row out from under the
    in-flight reconcile) it returns a ``superseded`` status, which is logged but
    NOT counted as processed work and never resurrects the row.
    """
    vault_id = claim["vault_id"]
    vault_name = claim["vault_name"]
    poll_retry_count = claim["poll_retry_count"]
    try:
        result = await _get_service().reconcile(vault_id, vault_name)
    except ExternalGitPolicyError as e:
        # A permanent policy violation at invocation time (Layer-2 re-validation
        # / exec-time re-guard) — quarantine, do not keep retrying a
        # remote that is no longer safe to touch. The exact expected-state CAS is
        # 'active' (the state this row was claimed in): if an operator RESET the
        # row to 'pending_preflight' with a NEW config mid-reconcile (a 3b
        # reconfigure), this stale quarantine matches 0 rows — superseded — and
        # never terminalizes the operator's fresh pending config.
        # A superseded quarantine (the row is already terminal, or was reset) is a
        # no-op — log it as such, never as a fresh one.
        quarantined = await ext_repo.quarantine(vault_id, _QUARANTINE_POLICY, "active")
        if not quarantined:
            logger.info(
                "External sync quarantine superseded (row already terminal): vault=%s",
                vault_name,
            )
            return 0
        logger.warning(
            "External sync quarantined: vault=%s reason=%s type=%s detail=%s",
            vault_name, _QUARANTINE_POLICY, type(e).__name__, _safe_detail(e),
        )
        return 0
    except Exception as e:  # noqa: BLE001 — keep loop alive; transient → backoff
        code, safe_message = _classify_failure(e)
        delay = next_attempt_delay(poll_retry_count)
        # Log the fixed code + exception TYPE (and a message only for types with
        # a guaranteed-safe message), never raw str(e).
        logger.warning(
            "External sync failed: vault=%s retry=%d backoff=%ds code=%s type=%s detail=%s",
            vault_name, poll_retry_count, delay, code, type(e).__name__, _safe_detail(e),
        )
        await ext_repo.mark_failure(vault_id, code, safe_message, delay, "active")
        return 0
    # reconcile succeeded internally. A 'superseded' status means its mark_success
    # CAS matched zero rows (the row was quarantined mid-reconcile): do not count
    # it as processed work; the terminal state is left untouched.
    if isinstance(result, dict) and result.get("status") == "superseded":
        logger.info(
            "External sync superseded (row no longer active): vault=%s", vault_name,
        )
        return 0
    return 1


async def _process_once() -> int:
    pool = await get_pool()
    ext_repo = VaultExternalGitRepository(pool)
    did = 0

    # One preflight (cheap: DNS + a DB write, no git fetch) — promotes a row into
    # 'active', keeping the reconcile queue fed. A row's own claim advances
    # poll_next_at, so this cannot re-claim the same row within the pass.
    async with pool.acquire() as conn:
        async with conn.transaction():
            preflight = await _claim_preflight(conn)
    if preflight is not None:
        did += await _run_preflight(preflight, ext_repo)

    # AND one reconcile (heavy: network + git fetch + per-file writes), attempted
    # UNCONDITIONALLY — even right after a preflight was processed. A strict
    # preflight-first return would let a steady influx of new pending_preflight
    # mirrors starve a due 'active' mirror of its reconcile indefinitely.
    # Capping each pass at one preflight + one reconcile keeps both
    # sides progressing; the loop respins immediately while either still has due
    # work. The extra empty _claim_reconcile probe is a cheap indexed LIMIT-1.
    async with pool.acquire() as conn:
        async with conn.transaction():
            reconcile = await _claim_reconcile(conn)
    if reconcile is not None:
        did += await _run_reconcile(reconcile, ext_repo)

    return did


_runner = BackfillRunner("external_git_poller", _process_once)
start = _runner.start
stop = _runner.stop


async def pending_stats() -> dict:
    """Snapshot for /health, keyed on the quarantine state machine.

    ``due`` / ``retrying`` / ``abandoned`` are scoped to ``active`` rows (the
    ones the reconcile path considers); ``pending_preflight`` counts rows still
    awaiting re-validation; ``quarantined`` counts rows an operator must review.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                                    AS total,
                COUNT(*) FILTER (WHERE sync_state = 'pending_preflight')    AS pending_preflight,
                COUNT(*) FILTER (WHERE sync_state = 'active')               AS active,
                COUNT(*) FILTER (WHERE sync_state = 'quarantined')          AS quarantined,
                COUNT(*) FILTER (WHERE sync_state = 'active'
                                 AND poll_next_at <= NOW()
                                 AND poll_retry_count < $1)                 AS due,
                COUNT(*) FILTER (WHERE sync_state = 'active'
                                 AND poll_retry_count > 0
                                 AND poll_retry_count < $1)                 AS retrying,
                COUNT(*) FILTER (WHERE sync_state = 'active'
                                 AND poll_retry_count >= $1)                AS abandoned
              FROM vault_external_git
            """,
            MAX_RETRIES,
        )
    return {
        "total":             int(row["total"]),
        "pending_preflight": int(row["pending_preflight"]),
        "active":            int(row["active"]),
        "quarantined":       int(row["quarantined"]),
        "due":               int(row["due"]),
        "retrying":          int(row["retrying"]),
        "abandoned":         int(row["abandoned"]),
    }
