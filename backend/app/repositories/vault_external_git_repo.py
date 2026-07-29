"""Repository for vault_external_git operations.

Sidecar 1:1 to `vaults` — only present for vaults that mirror an
external git repo. All access guards, polling, and reconcile bookkeeping
go through here so the rest of the codebase doesn't grow ad-hoc SQL
against this table.
"""

from __future__ import annotations

import uuid

import asyncpg


# Fixed, credential-free placeholder written into ``remote_url`` when the stored
# URL is too malformed for ``urlsplit`` to parse (so its userinfo cannot be
# reliably split out). It replaces the raw value ATOMICALLY as part of the
# quarantine so no credential can linger in the terminal row even if the
# malformed authority embedded one. Value-less by
# construction.
_MALFORMED_URL_SENTINEL = "removed://malformed-external-git-url"


def _updated_one(status: str | None) -> bool:
    """Whether an asyncpg UPDATE command tag reports at least one affected row.

    A CAS'd state transition filters on the EXPECTED
    ``sync_state``; when a peer / operator moved the row out from under us the
    UPDATE matches zero rows and asyncpg returns ``"UPDATE 0"``. Callers treat a
    ``False`` here as "the transition was stale — discard, do not retry"."""
    try:
        return int(status.split()[-1]) > 0  # type: ignore[union-attr]
    except (AttributeError, ValueError, IndexError):
        return False


def _snapshot_clause(url_idx: int, token_idx: int) -> str:
    """The optional snapshot old-value guard: a
    state transition fires only when the row STILL holds the exact
    ``(remote_url, auth_token)`` the caller just validated, so a concurrent
    operator reconfigure is a 0-row no-op (superseded) that never clobbers the
    new config with a stale validation result. Combined with the transition's
    expected-``sync_state`` guard this is a full snapshot-CAS: "transition only
    if this is byte-for-byte the config I checked, and it is still in the state I
    claimed it in". ``auth_token`` uses ``IS NOT DISTINCT FROM`` so a NULL column
    matches a NULL validated token (a plain ``=`` would never match a NULL)."""
    return (
        f" AND remote_url = ${url_idx} "
        f"AND auth_token IS NOT DISTINCT FROM ${token_idx}"
    )


class VaultExternalGitRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        vault_id: uuid.UUID,
        remote_url: str,
        remote_branch: str,
        auth_token: str | None,
        poll_interval_secs: int,
        conn=None,
    ) -> None:
        sql = """
            INSERT INTO vault_external_git
                (vault_id, remote_url, remote_branch, auth_token, poll_interval_secs)
            VALUES ($1, $2, $3, $4, $5)
        """
        args = (vault_id, remote_url, remote_branch, auth_token, poll_interval_secs)
        if conn is not None:
            await conn.execute(sql, *args)
        else:
            async with self.pool.acquire() as acq:
                await acq.execute(sql, *args)

    async def get(self, vault_id: uuid.UUID) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM vault_external_git WHERE vault_id = $1",
                vault_id,
            )
            return dict(row) if row else None

    async def exists(self, vault_id: uuid.UUID) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM vault_external_git WHERE vault_id = $1",
                vault_id,
            ))

    async def list_mirror_vault_names(self) -> list[str]:
        """Every vault NAME that has a ``vault_external_git`` row — i.e. every
        external-git mirror. The DB is the authoritative record of which vaults
        are mirrors, so this drives the startup marker backfill (
        ``external_git_service.backfill_mirror_markers``) that
        re-establishes the on-disk mirror marker for mirrors predating it."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT v.name
                  FROM vault_external_git veg
                  JOIN vaults v ON v.id = veg.vault_id
                """
            )
        return [r["name"] for r in rows]

    async def mark_success(
        self,
        vault_id: uuid.UUID,
        poll_interval_secs: int,
        new_sha: str | None = None,
        *,
        validated_url: str | None = None,
        validated_token: str | None = None,
    ) -> bool:
        """Successful reconcile or no-op (unchanged HEAD). When `new_sha`
        is provided the cursor advances; when None (remote HEAD matched
        existing cursor) only the next-poll schedule resets. Returns whether a
        row actually transitioned (see the CAS note below).

        Writes the hardened poller's OWN schedule columns (``poll_next_at`` /
        ``poll_retry_count``). The legacy ``next_attempt_at`` / ``retry_count``
        columns are the rollout fence (migration 049) and are deliberately NOT
        touched.

        Snapshot-CAS: the UPDATE
        is guarded on ``sync_state = 'active'`` AND — when the caller passes
        ``validated_url`` — on the exact ``(remote_url, auth_token)`` the
        reconcile actually fetched against. So a reconcile that finished on a row
        an operator has since QUARANTINED cannot silently resurrect its
        bookkeeping (state guard), and one that finished after an operator
        RECONFIGURED the remote cannot advance the cursor for a config it never
        synced (old-value guard) — either is a 0-row no-op → ``False`` →
        superseded. The reconcile-claim already proved the row 'active', so in
        the normal path exactly one row matches and ``sync_state`` stays 'active'.
        """
        async with self.pool.acquire() as conn:
            if new_sha is None:
                args: list = [vault_id, str(poll_interval_secs)]
                guard = ""
                if validated_url is not None:
                    guard = _snapshot_clause(len(args) + 1, len(args) + 2)
                    args.extend([validated_url, validated_token])
                status = await conn.execute(
                    f"""
                    UPDATE vault_external_git
                       SET last_error       = NULL,
                           poll_retry_count  = 0,
                           poll_next_at      = NOW() + ($2 || ' seconds')::interval,
                           updated_at        = NOW()
                     WHERE vault_id = $1
                       AND sync_state = 'active'{guard}
                    """,
                    *args,
                )
            else:
                args = [vault_id, new_sha, str(poll_interval_secs)]
                guard = ""
                if validated_url is not None:
                    guard = _snapshot_clause(len(args) + 1, len(args) + 2)
                    args.extend([validated_url, validated_token])
                status = await conn.execute(
                    f"""
                    UPDATE vault_external_git
                       SET last_synced_sha   = $2,
                           last_synced_at    = NOW(),
                           last_error        = NULL,
                           poll_retry_count  = 0,
                           poll_next_at      = NOW() + ($3 || ' seconds')::interval,
                           updated_at        = NOW()
                     WHERE vault_id = $1
                       AND sync_state = 'active'{guard}
                    """,
                    *args,
                )
        return _updated_one(status)

    async def mark_failure(
        self,
        vault_id: uuid.UUID,
        code: str,
        safe_message: str,
        backoff_secs: int,
        expected_state: str,
        *,
        validated_url: str | None = None,
        validated_token: str | None = None,
    ) -> bool:
        """Record a TRANSIENT failure and back off — used by both the reconcile
        and the preflight paths. Advances the hardened poller's own
        ``poll_next_at`` and increments ``poll_retry_count``; ``sync_state`` is
        left unchanged, so a reconcile failure stays 'active' and a preflight
        failure stays 'pending_preflight'. PERMANENT policy violations never come
        here — they go to :meth:`quarantine`. The legacy fence columns are not
        touched. Returns whether a row actually transitioned.

        Type-enforced safe-failure boundary: the caller MUST
        pass a FIXED error ``code`` and a STATIC, secret-free ``safe_message`` —
        never a raw exception string (which may carry an un-sanitized remote URL
        or git stderr). Both are non-optional so a caller cannot accidentally
        route ``str(e)`` here; they are persisted as ``[code] safe_message`` in
        ``last_error`` and truncated.

        Snapshot-CAS: the caller
        passes the ``expected_state`` the row must still be in ('pending_preflight'
        for a preflight backoff, 'active' for a reconcile backoff) and — on the
        preflight path — the ``(validated_url, validated_token)`` it validated.
        The UPDATE is guarded on both, so a backoff can never write over a row
        that was quarantined (state guard) or reconfigured (old-value guard) out
        from under the in-flight op — a stale backoff matches zero rows and
        returns ``False``.
        """
        if not isinstance(code, str) or not isinstance(safe_message, str):
            raise TypeError("mark_failure requires (code: str, safe_message: str)")
        if not isinstance(expected_state, str):
            raise TypeError("mark_failure requires (expected_state: str)")
        stored = f"[{code}] {safe_message}"[:500]
        args: list = [vault_id, stored, str(backoff_secs), expected_state]
        guard = ""
        if validated_url is not None:
            guard = _snapshot_clause(len(args) + 1, len(args) + 2)
            args.extend([validated_url, validated_token])
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                f"""
                UPDATE vault_external_git
                   SET last_error       = $2,
                       poll_retry_count  = poll_retry_count + 1,
                       poll_next_at      = NOW() + ($3 || ' seconds')::interval,
                       updated_at        = NOW()
                 WHERE vault_id = $1
                   AND sync_state = $4{guard}
                """,
                *args,
            )
        return _updated_one(status)

    async def scrub_legacy_credential(
        self,
        vault_id: uuid.UUID,
        scrubbed_url: str,
        new_auth_token: str | None,
        claimed_url: str,
        claimed_token: str | None,
    ) -> bool:
        """Persist a credential-free ``remote_url`` (+ migrated token) for a mirror
        whose stored URL embedded a legacy credential, BEFORE any DNS/network work.

        The preflight path strips any userinfo embedded in a legacy ``remote_url``
        in memory; without this write the raw ``x-access-token:TOKEN@host`` (or a
        ``user:pass@`` form) would linger in the DB whenever the row is then
        quarantined or merely backed off (transient), because the URL/token were
        previously only persisted on the SUCCESS path (:meth:`activate_from_preflight`).
        Writing the scrubbed URL up front means EVERY downstream branch
        (quarantine / transient backoff / activate) leaves no credential behind.

        OLD-VALUE CAS, not a state CAS: guarded on the
        CLAIMED ``(remote_url, auth_token)`` the caller observed — deliberately
        STATE-INDEPENDENT, and it never rewrites ``sync_state`` /
        ``sync_state_reason`` (a pure hygiene write). This closes two residue
        paths a ``sync_state = 'pending_preflight'`` CAS left open:

        * a row an operator/peer QUARANTINED between the claim and this write
          would match zero rows under a state CAS, stranding the raw credential in
          the now-terminal row forever (no later branch could reach it). Quarantine
          leaves ``remote_url`` / ``auth_token`` untouched, so this old-value CAS
          still matches and scrubs it;
        * conversely, an operator who concurrently RECONFIGURED the URL/token is a
          0-row no-op here — the old values no longer match, so a stale scrub can
          never clobber a newer setting. On a 0-row result the preflight caller
          RELOADS the current row and re-classifies it from scratch (never
          proceeding with the stale claimed value), so no credential is left
          behind either way.

        ``auth_token`` is compared with ``IS NOT DISTINCT FROM`` so a NULL column
        matches a NULL ``claimed_token`` (a plain ``=`` would make every
        NULL-token row unmatchable). Returns whether a row was scrubbed.
        """
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE vault_external_git
                   SET remote_url = $2,
                       auth_token = $3,
                       updated_at = NOW()
                 WHERE vault_id = $1
                   AND remote_url = $4
                   AND auth_token IS NOT DISTINCT FROM $5
                """,
                vault_id, scrubbed_url, new_auth_token, claimed_url, claimed_token,
            )
        return _updated_one(status)

    async def activate_from_preflight(
        self,
        vault_id: uuid.UUID,
        canonical_url: str,
        auth_token: str | None,
        poll_interval_secs: int,
        *,
        validated_url: str | None = None,
        validated_token: str | None = None,
    ) -> bool:
        """Promote a 'pending_preflight' row to 'active' after a passing Layer-2
        re-validation + legacy credential scrub. Returns whether a
        row actually transitioned.

        Persists the SCRUBBED canonical URL (any embedded userinfo removed) and
        the effective auth token (a supported ``x-access-token:TOKEN`` URL
        credential is moved out of the URL and into this column), clears any
        prior error/quarantine reason, and schedules an immediate reconcile
        (``poll_next_at = NOW()``). ``canonical_url`` is the validator's
        credential-free output; it never carries a secret.

        Snapshot-CAS: guarded on
        ``sync_state = 'pending_preflight'`` AND on the exact
        ``(validated_url, validated_token)`` this preflight scrubbed + validated.
        So a slow preflight cannot activate a row an operator has since
        QUARANTINED (state guard) NOR overwrite a remote an operator RECONFIGURED
        mid-flight with the now-stale validated URL/token (old-value guard) — a
        stale activation matches zero rows and returns ``False``, keeping the
        operator's new config intact for the next preflight to re-validate.
        """
        args: list = [vault_id, canonical_url, auth_token]
        guard = ""
        if validated_url is not None:
            guard = _snapshot_clause(len(args) + 1, len(args) + 2)
            args.extend([validated_url, validated_token])
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                f"""
                UPDATE vault_external_git
                   SET remote_url        = $2,
                       auth_token        = $3,
                       sync_state        = 'active',
                       sync_state_reason = NULL,
                       sync_state_at     = NOW(),
                       poll_next_at      = NOW(),
                       poll_retry_count  = 0,
                       last_error        = NULL,
                       updated_at        = NOW()
                 WHERE vault_id = $1
                   AND sync_state = 'pending_preflight'{guard}
                """,
                *args,
            )
        return _updated_one(status)

    async def quarantine(
        self,
        vault_id: uuid.UUID,
        reason_code: str,
        expected_state: str,
        *,
        validated_url: str | None = None,
        validated_token: str | None = None,
    ) -> bool:
        """Move a row to 'quarantined' on a PERMANENT policy violation — a bad
        scheme/host/branch or an unscrubbable legacy URL credential. Quarantined
        rows are claimed by neither poller path; an operator must inspect and
        re-enable. Returns whether a row actually transitioned.

        ``reason_code`` MUST be a FIXED, secret-free enum value:
        never a raw URL, token, or exception string. It is persisted verbatim in
        ``sync_state_reason`` and echoed (bracketed) into ``last_error``.

        Exact expected-state CAS: the caller
        passes the EXACT ``sync_state`` it claimed the row in — 'pending_preflight'
        for the preflight path, 'active' for the reconcile path — and the UPDATE
        fires only from THAT state (``sync_state = $expected_state``), not merely
        "pending OR active". This closes the last edge: if a slow reconcile hits a
        policy error AFTER an operator RESET the row to 'pending_preflight' with a
        NEW config (a 3b reconfigure), the reconcile's state-only
        ``quarantine(expected_state='active')`` matches 0 rows — superseded —
        rather than terminalizing (and thereby DESTROYING) the operator's fresh
        pending config. It also keeps quarantine a fully TERMINAL, IMMUTABLE state:
        an already-quarantined row equals neither 'pending_preflight' nor 'active',
        so a stale re-quarantine (a converging peer, or a preflight/reconcile
        racing an operator quarantine) is a 0-row no-op that PRESERVES the FIRST
        reason and never bumps the timestamp.

        Snapshot-CAS: when the preflight
        caller ALSO passes ``(validated_url, validated_token)`` the UPDATE requires
        the row still hold that exact config too, so a policy quarantine derived
        from a now-stale validation cannot terminalize a remote an operator
        RECONFIGURED to a (possibly safe) new value mid-flight — that is a 0-row
        no-op and the caller reloads + re-classifies the current config. The
        reconcile path omits the old-value guard (exact-state guard only).
        """
        if not isinstance(reason_code, str):
            raise TypeError("quarantine requires (reason_code: str)")
        if not isinstance(expected_state, str):
            raise TypeError("quarantine requires (expected_state: str)")
        args: list = [
            vault_id, reason_code, f"[quarantine] {reason_code}"[:500], expected_state
        ]
        guard = ""
        if validated_url is not None:
            guard = _snapshot_clause(len(args) + 1, len(args) + 2)
            args.extend([validated_url, validated_token])
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                f"""
                UPDATE vault_external_git
                   SET sync_state        = 'quarantined',
                       sync_state_reason = $2,
                       sync_state_at     = NOW(),
                       last_error        = $3,
                       updated_at        = NOW()
                 WHERE vault_id = $1
                   AND sync_state = $4{guard}
                """,
                *args,
            )
        return _updated_one(status)

    async def redact_malformed_and_quarantine(
        self,
        vault_id: uuid.UUID,
        claimed_url: str,
        claimed_token: str | None,
        reason_code: str,
    ) -> bool:
        """Redact an unparseable ``remote_url`` to a fixed, credential-free
        sentinel AND quarantine it in ONE ATOMIC UPDATE.

        A URL ``urlsplit`` cannot parse is a PERMANENT syntax violation, but its
        userinfo cannot be reliably stripped — so quarantining it verbatim would
        strand any embedded credential in the terminal row. Doing the redact and
        the quarantine as two separate writes (as an earlier revision did) left a
        window where an operator reconfigure could slip a NEW credential-bearing
        URL between them: the old-value scrub matched 0 rows, then a state-only
        quarantine terminalized the row with the new credential still in it. This
        single UPDATE closes that: the redact and the state transition are
        indivisible.

        Snapshot-CAS (old-value + exact state): guarded on the CLAIMED
        ``(remote_url, auth_token)`` and ``sync_state = 'pending_preflight'`` — a
        malformed URL is only ever classified from the preflight path, so this is
        the one state it can be quarantined from (consistent with :meth:`quarantine`'s
        exact expected-state CAS). A concurrent operator reconfigure (old value
        gone) is a 0-row no-op — the caller reloads + re-classifies the CURRENT
        config rather than terminalizing a stale one. ``auth_token`` is set NULL: a
        malformed URL is unparseable, so nothing about any embedded credential can
        be trusted or migrated, and the terminal row must carry no secret in EITHER
        column. ``reason_code`` is a FIXED, secret-free enum. Returns
        whether a row was quarantined.
        """
        if not isinstance(reason_code, str):
            raise TypeError(
                "redact_malformed_and_quarantine requires (reason_code: str)"
            )
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE vault_external_git
                   SET remote_url        = $2,
                       auth_token        = NULL,
                       sync_state        = 'quarantined',
                       sync_state_reason = $3,
                       sync_state_at     = NOW(),
                       last_error        = $4,
                       updated_at        = NOW()
                 WHERE vault_id = $1
                   AND remote_url = $5
                   AND auth_token IS NOT DISTINCT FROM $6
                   AND sync_state = 'pending_preflight'
                """,
                vault_id, _MALFORMED_URL_SENTINEL, reason_code,
                f"[quarantine] {reason_code}"[:500],
                claimed_url, claimed_token,
            )
        return _updated_one(status)

    async def redact_malformed_url_if_quarantined(
        self,
        vault_id: uuid.UUID,
        claimed_url: str,
        claimed_token: str | None,
    ) -> bool:
        """Hygiene: strip a credential-bearing malformed ``remote_url`` (→ the
        fixed sentinel, ``auth_token`` → NULL) from a row that is ALREADY
        'quarantined' with that same old value, WITHOUT touching
        ``sync_state_reason`` / ``sync_state_at``.

        Used only when :meth:`redact_malformed_and_quarantine` matched 0 rows
        because a peer/operator already quarantined this EXACT config (for some
        other reason) but may have left the raw credential in ``remote_url``. This
        removes the secret while PRESERVING the first quarantine reason/timestamp,
        so quarantine stays terminal + immutable. Guarded on the claimed old value
        so it never rewrites a row the operator has since reconfigured. Returns
        whether a row was redacted.
        """
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE vault_external_git
                   SET remote_url = $2,
                       auth_token = NULL,
                       updated_at = NOW()
                 WHERE vault_id = $1
                   AND remote_url = $3
                   AND auth_token IS NOT DISTINCT FROM $4
                   AND sync_state = 'quarantined'
                """,
                vault_id, _MALFORMED_URL_SENTINEL, claimed_url, claimed_token,
            )
        return _updated_one(status)
