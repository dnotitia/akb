"""Pending admissions — the arrival ``invite_only`` refuses, kept.

``invite_only`` admits an exact ``(issuer, subject)`` pair and refuses everyone
else. The subject half of that pair is assigned by this workspace's own realm,
and it comes into being at exactly one moment: the person's first arrival
through the broker. Before that arrival nobody can name it. That is why every
attempt to bind an invited person *ahead* of time has failed — each one was
trying to obtain a value that did not exist yet.

So the refusal records what it refused. Nothing else about the refusal changes:
the caller still receives ``membership_required`` and holds nothing. The record
authorizes nothing; it is what an administrator reads in order to approve one
specific arrival, after which the ordinary exact-binding path
(``ensure_human_external_identity``) writes the binding.

Two properties are load-bearing and are asserted by tests rather than assumed:

* **Recording cannot rescue a refusal.** If the record cannot be written, the
  refusal is still a refusal. A pending admission is a note, so failing to take
  a note must never become a way in — nor a 500 that hides the real answer.
* **Nothing is keyed by email.** The address is stored because an administrator
  reads it. Approval names a row, and that row carries the subject.

The table is bounded twice, and both bounds are on the record rather than on
the refusal. Producing an arrival costs an authenticated session at the
upstream identity provider, so the table is not reachable by anyone who merely
reaches the login page; the bounds exist because "unbounded but hard to fill"
is still unbounded.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import NotFoundError, ValidationError


_LOG = logging.getLogger("akb.admission")


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


async def record_arrival(
    conn,
    *,
    issuer: str,
    subject: str,
    claims: dict[str, Any],
) -> None:
    """Note that this exact identity presented itself and was refused.

    Runs on the connection the refusal already holds, outside any transaction
    of its own, and never raises: a failure to take a note must not change what
    the caller is told. The refusal is raised by the caller immediately after
    this returns, whatever happened here.
    """
    email = _text(claims.get("email"))
    display_name = _text(claims.get("name")) or _text(claims.get("preferred_username"))
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO pending_admissions (issuer, subject, email, display_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT ON CONSTRAINT pending_admissions_issuer_subject_key
                DO UPDATE SET
                    email = EXCLUDED.email,
                    display_name = EXCLUDED.display_name,
                    last_seen_at = NOW(),
                    arrivals = pending_admissions.arrivals + 1
                """,
                issuer,
                subject,
                email,
                display_name,
            )
            # Eviction runs with the write so the table stays bounded without a
            # scheduled job that a deployment could be missing. Both clauses
            # keep the most recent arrivals, which is the row just written.
            await conn.execute(
                """
                DELETE FROM pending_admissions
                 WHERE last_seen_at < NOW() - ($1 || ' hours')::interval
                """,
                str(int(settings.keycloak_pending_admission_retention_hours)),
            )
            await conn.execute(
                """
                DELETE FROM pending_admissions
                 WHERE id IN (
                     SELECT id FROM pending_admissions
                      ORDER BY last_seen_at DESC, id
                      OFFSET $1
                 )
                """,
                int(settings.keycloak_pending_admission_cap),
            )
    except Exception as exc:  # noqa: BLE001 — see the docstring: never rescue, never 500
        _LOG.warning(
            "pending admission not recorded for an arrival that was refused (%s)", exc
        )


def _row(record) -> dict[str, Any]:
    return {
        "id": str(record["id"]),
        "issuer": record["issuer"],
        "subject": record["subject"],
        "email": record["email"],
        "display_name": record["display_name"],
        "first_seen_at": record["first_seen_at"].isoformat(),
        "last_seen_at": record["last_seen_at"].isoformat(),
        "arrivals": record["arrivals"],
    }


async def list_pending_admissions(*, limit: int = 200) -> dict[str, Any]:
    """Arrivals an administrator can still act on, most recent first."""
    bounded = max(1, min(int(limit), 1000))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, issuer, subject, email, display_name,
                   first_seen_at, last_seen_at, arrivals
              FROM pending_admissions
             WHERE last_seen_at >= NOW() - ($2 || ' hours')::interval
             ORDER BY last_seen_at DESC, id
             LIMIT $1
            """,
            bounded,
            str(int(settings.keycloak_pending_admission_retention_hours)),
        )
    return {"pending_admissions": [_row(row) for row in rows]}


def _admission_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise ValidationError("pending admission id must be a UUID") from None


async def get_pending_admission(admission_id: str) -> dict[str, Any]:
    identifier = _admission_uuid(admission_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, issuer, subject, email, display_name,
                   first_seen_at, last_seen_at, arrivals
              FROM pending_admissions
             WHERE id = $1
            """,
            identifier,
        )
    if row is None:
        raise NotFoundError("Pending admission", str(identifier))
    return _row(row)


async def dismiss_pending_admission(admission_id: str) -> dict[str, Any]:
    """Forget one arrival. It says nothing about whether they may return."""
    identifier = _admission_uuid(admission_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM pending_admissions WHERE id = $1 RETURNING id",
            identifier,
        )
    if row is None:
        raise NotFoundError("Pending admission", str(identifier))
    return {"dismissed": str(row["id"])}


async def approve_pending_admission(
    admission_id: str,
    *,
    actor_id: str,
    existing_user_id: str | None = None,
    email: str | None = None,
    display_name: str | None = None,
    prepare_suspended: bool = False,
) -> dict[str, Any]:
    """Bind one recorded arrival to an AKB account.

    The subject is taken from the ROW, never from the caller: naming the row is
    the whole act of approval, and a caller that could also supply a subject
    could approve one arrival while binding another. ``existing_user_id``
    attaches the arrival to an account that already exists — the invited
    person's prepared account, or, during a workspace's move to its own realm,
    the account they have been using all along.

    The binding itself is the ordinary exact-binding path, so the issuer guard,
    the conflict rules, and the audit trail are the same ones every other
    binding gets. In particular an arrival recorded under an issuer this
    runtime does not present is refused here rather than written.

    The row is removed only after the binding succeeds. A failed approval
    leaves the arrival exactly where it was.
    """
    from app.services.account_service import ensure_human_external_identity

    arrival = await get_pending_admission(admission_id)
    resolved_email = _text(email) or _text(arrival["email"])
    if resolved_email is None:
        # `ensure_human_external_identity` requires one, and an arrival whose
        # provider sent no email claim cannot supply it. Say which of the two
        # is missing rather than failing as a generic invalid argument.
        raise ValidationError(
            "This arrival carried no email address; supply one to approve it"
        )
    # The exact-binding path removes the note itself, so every writer of a
    # binding answers the arrival it belongs to — not only this one.
    result = await ensure_human_external_identity(
        issuer=arrival["issuer"],
        subject=arrival["subject"],
        email=resolved_email,
        display_name=_text(display_name) or _text(arrival["display_name"]),
        existing_user_id=existing_user_id,
        prepare_suspended=prepare_suspended,
        actor_id=actor_id,
    )
    return {"approved": arrival, "user": result}
