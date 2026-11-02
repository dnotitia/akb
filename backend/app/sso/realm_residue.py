"""Realm-account residue: what the installation realm holds that AKB does not.

When a person signs in through a brokered provider, Keycloak creates an account
in this installation's realm before AKB decides anything. If AKB then refuses
the arrival — ``invite_only``, or before an administrator admits them — it
keeps a note of the refusal, but the realm account stays. AKB's permanent
management client holds only read-only user roles, so it cannot remove that
account, and until now nothing counted it.

This module is that count, and nothing more::

    residue = realm accounts - (exact bindings | recorded arrivals)

* An **exact binding** is a row in ``external_identities`` for this realm's
  issuer and the account's id (the ``sub`` the realm issues for it).
* A **recorded arrival** is a ``pending_admissions`` row for the same pair that
  an administrator can still act on, which is the window
  ``list_pending_admissions`` uses. A row past retention is about to be
  evicted and is not listed to anyone, so it does not excuse the account.

Every unbound account is listed with its arrival record, if any, so an
administrator can tell an account that is merely awaiting approval from one
nothing in AKB refers to. The second kind is the residue.

Deliberately absent: deleting or expiring anything. Removing a realm account
needs authority a running AKB does not hold, and an old account can belong to a
person whose admission is only pending.

A failed realm read is reported as ``unavailable`` with no counts at all. A
zero is a measurement; it must never stand in for a read that did not happen.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import settings
from app.db.postgres import get_pool
from app.sso.keycloak_admin import ProviderControlError
from app.sso.models import RealmAccount, RealmAccountListing


REALM_RESIDUE_SCHEMA_VERSION = 1
# How many realm accounts one read may examine. A larger realm is reported as
# truncated, and every count then describes only the accounts that were read.
REALM_ACCOUNT_SCAN_LIMIT = 1000
# How many unbound accounts the payload lists. The counts are not bounded by
# this; residue is listed ahead of pending arrivals, oldest first.
REALM_RESIDUE_LIST_LIMIT = 200


@dataclass(frozen=True, slots=True)
class ArrivalRecord:
    """A reference to the recorded arrival of one realm account."""

    admission_id: str
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class UnboundAccount:
    account: RealmAccount
    arrival: ArrivalRecord | None


@dataclass(frozen=True, slots=True)
class RealmResidue:
    realm_account_count: int
    truncated: bool
    bound_count: int
    pending_arrival_count: int
    residue_count: int
    accounts: tuple[UnboundAccount, ...]


def reconcile(
    listing: RealmAccountListing,
    *,
    bound_subjects: frozenset[str],
    arrivals: Mapping[str, ArrivalRecord],
    list_limit: int,
) -> RealmResidue:
    """Partition the realm's accounts into bound, pending, and residue.

    Only accounts the realm listed are counted: a binding or an arrival whose
    subject the realm no longer holds changes nothing here. A binding wins over
    an arrival, so every account lands in exactly one partition.
    """
    bound = 0
    unbound: list[UnboundAccount] = []
    for account in listing.accounts:
        if account.user_id in bound_subjects:
            bound += 1
            continue
        unbound.append(UnboundAccount(account=account, arrival=arrivals.get(account.user_id)))
    residue = sum(1 for item in unbound if item.arrival is None)
    ordered = sorted(
        unbound,
        key=lambda item: (
            item.arrival is not None,
            item.account.created_at is None,
            item.account.created_at.timestamp() if item.account.created_at is not None else 0.0,
            item.account.user_id,
        ),
    )
    return RealmResidue(
        realm_account_count=len(listing.accounts),
        truncated=listing.truncated,
        bound_count=bound,
        pending_arrival_count=len(unbound) - residue,
        residue_count=residue,
        accounts=tuple(ordered[: max(0, list_limit)]),
    )


async def read_akb_records(
    issuer: str,
    subjects: tuple[str, ...],
) -> tuple[frozenset[str], dict[str, ArrivalRecord]]:
    """Read the exact bindings and live recorded arrivals for these subjects."""
    if not subjects:
        return frozenset(), {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        bound_rows = await conn.fetch(
            """
            SELECT subject
              FROM external_identities
             WHERE issuer = $1 AND subject = ANY($2::text[])
            """,
            issuer,
            list(subjects),
        )
        arrival_rows = await conn.fetch(
            """
            SELECT id, subject, last_seen_at
              FROM pending_admissions
             WHERE issuer = $1
               AND subject = ANY($2::text[])
               AND last_seen_at >= NOW() - ($3 || ' hours')::interval
            """,
            issuer,
            list(subjects),
            str(int(settings.keycloak_pending_admission_retention_hours)),
        )
    return (
        frozenset(row["subject"] for row in bound_rows),
        {
            row["subject"]: ArrivalRecord(
                admission_id=str(row["id"]),
                last_seen_at=row["last_seen_at"],
            )
            for row in arrival_rows
        },
    )


def _unavailable(code: str) -> dict[str, Any]:
    return {
        "schema_version": REALM_RESIDUE_SCHEMA_VERSION,
        "state": "unavailable",
        "unavailable_code": code,
        "scan_limit": REALM_ACCOUNT_SCAN_LIMIT,
        "truncated": None,
        "realm_account_count": None,
        "bound_count": None,
        "pending_arrival_count": None,
        "residue_count": None,
        "accounts": None,
    }


def _account_view(item: UnboundAccount) -> dict[str, Any]:
    return {
        "realm_user_id": item.account.user_id,
        "username": item.account.username,
        "created_at": (
            item.account.created_at.isoformat() if item.account.created_at is not None else None
        ),
        "arrival": (
            {
                "admission_id": item.arrival.admission_id,
                "last_seen_at": item.arrival.last_seen_at.isoformat(),
            }
            if item.arrival is not None
            else None
        ),
    }


async def realm_account_residue(control: Any) -> dict[str, Any]:
    """Return the versioned residue payload for the product-admin surface.

    ``control`` is the Keycloak provider control. Only its read-only user
    listing is called; any failure of that read becomes ``unavailable``.
    """
    if control.control_mode != "direct":
        return _unavailable("keycloak_provider_control_delegated")
    issuer = settings.keycloak_issuer
    if not issuer or control.config.broker_issuer != issuer:
        # Bindings are keyed by the issuer this runtime presents. Reconciling a
        # realm under any other issuer would call every account residue.
        return _unavailable("realm_residue_authority_mismatch")
    try:
        listing = await control.list_realm_accounts(limit=REALM_ACCOUNT_SCAN_LIMIT)
    except ProviderControlError as error:
        return _unavailable(error.code)
    bound_subjects, arrivals = await read_akb_records(
        issuer, tuple(account.user_id for account in listing.accounts)
    )
    residue = reconcile(
        listing,
        bound_subjects=bound_subjects,
        arrivals=arrivals,
        list_limit=REALM_RESIDUE_LIST_LIMIT,
    )
    return {
        "schema_version": REALM_RESIDUE_SCHEMA_VERSION,
        "state": "available",
        "unavailable_code": None,
        "scan_limit": REALM_ACCOUNT_SCAN_LIMIT,
        "truncated": residue.truncated,
        "realm_account_count": residue.realm_account_count,
        "bound_count": residue.bound_count,
        "pending_arrival_count": residue.pending_arrival_count,
        "residue_count": residue.residue_count,
        "accounts": [_account_view(item) for item in residue.accounts],
    }
