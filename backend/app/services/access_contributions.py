"""Why a (vault, user) pair holds the role it holds.

``vault_access`` stores the *result* of a grant, not the grant. One row per
pair, and ``grant_access`` overwrites ``role`` on conflict. That is correct
while every grant is one person acting on another — there is only ever one
reason then — and it stops being correct the moment a second, automated
grantor exists:

    1. a person holds ``reader`` on V, granted directly;
    2. an automated grantor applies ``writer`` for everyone in some set the
       person belongs to; the row becomes ``writer`` and the direct ``reader``
       no longer exists anywhere;
    3. the person leaves the set, the grantor revokes, and the row is deleted.

Nobody revoked the access held in step 1, and after step 2 no query could have
prevented step 3. This module holds the reasons so that it can.

``vault_access`` stays the materialized effective row and is recomputed here
inside the caller's transaction, so every reader keeps working unchanged — REST,
``akb_sql``, search, grep, revision visibility, and the PostgreSQL role
membership ``role_sync`` reconciles out of ``vault_access``.

Deliberately outside this plane: ownership, ``public_access``, system
administration and the vault write policy. ``check_vault_access`` decides those
on separate branches and reports them through ``role_source``; folding them in
would create states the model cannot express ("revoke the ownership
contribution" is not a sentence) and would put break-glass behind a derivation.

AKB validates the *shape* of a source key and never interprets its value. The
moment it interprets one it has imported the grantor's concept, and the whole
point is that AKB need not know what anybody's rule is — only to keep
independent bases apart.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Iterable


# The one role ordering. `access_service` imports it from here so the
# derivation below and every comparison in the codebase read the same table.
ROLE_HIERARCHY = {"owner": 4, "admin": 3, "writer": 2, "reader": 1}

#: The basis every human grant carries, and the default for callers that do not
#: name one — which is every caller that existed before contributions did.
DIRECT_SOURCE_KEY = "direct"

#: `owner` is not among them: ownership lives on `vaults.owner_id`, and the only
#: `admin` this plane writes for a former owner comes from `transfer_ownership`.
CONTRIBUTION_ROLES = frozenset({"reader", "writer", "admin"})

_SOURCE_KEY_MAX = 255
_SOURCE_KEY_FORBIDDEN = re.compile(r"\s")


class InvalidSourceKey(ValueError):
    """The source key is not a shape AKB will store."""


def role_level(role: str | None) -> int:
    return ROLE_HIERARCHY.get(role or "", 0)


def effective_role(roles: Iterable[str | None]) -> str | None:
    """The strongest currently applicable role, or None if there is no basis.

    This is the derivation. It is one function on purpose: negative
    contributions, expiry, a coarser or finer grain and a different subject
    shape all reduce to "the union is computed differently", and each of them
    should be a change here rather than a sweep across call sites.
    """
    best: str | None = None
    for role in roles:
        if role_level(role) > role_level(best):
            best = role
    return best


def validate_source_key(source_key: str) -> str:
    """Shape only — never meaning.

    A registry of permitted namespaces would only stop two independent grantors
    choosing the same key, which is an operator configuration concern, and it
    can be added later as a validation without changing the row shape.
    """
    if not isinstance(source_key, str) or not source_key:
        raise InvalidSourceKey("source key must be a non-empty string")
    if len(source_key) > _SOURCE_KEY_MAX:
        raise InvalidSourceKey(
            f"source key exceeds {_SOURCE_KEY_MAX} characters"
        )
    if _SOURCE_KEY_FORBIDDEN.search(source_key):
        raise InvalidSourceKey("source key must not contain whitespace")
    return source_key


@dataclass(frozen=True)
class ContributionOutcome:
    """What one contribution write did, and what it did to the pair.

    `applied` is False when a stale revision made the write a no-op. The two
    effective roles are what an event subscriber needs: "the writer
    contribution was removed" and "this user is no longer a writer" are
    different facts once bases can coexist, and only the pair of values
    separates them.
    """

    applied: bool
    effective_role: str | None
    previous_effective_role: str | None
    contribution_role: str | None

    @property
    def effective_role_changed(self) -> bool:
        return self.effective_role != self.previous_effective_role


async def _lock_vault(conn, vault_id) -> None:
    """Serialize recompute for this vault.

    The vault row is the lock `grant_access`/`revoke_access` already take, and
    re-acquiring it inside their transaction is a no-op. Locking the pair's
    contribution rows instead would not serialize against a concurrent INSERT
    of a *new* basis for the same pair — there is no row yet to lock.
    """
    await conn.fetchval("SELECT id FROM vaults WHERE id = $1 FOR UPDATE", vault_id)


async def current_effective_role(conn, vault_id, user_id) -> str | None:
    """The stored effective role for a pair, as `vault_access` holds it."""
    return await conn.fetchval(
        "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
        vault_id, user_id,
    )


async def list_contributions(conn, vault_id, user_id) -> list[dict]:
    """Every basis on which this pair currently holds access."""
    rows = await conn.fetch(
        """
        SELECT c.source_key, c.role, c.revision, c.granted_by,
               c.created_at, c.updated_at, u.username AS granted_by_username
        FROM vault_access_contributions c
        LEFT JOIN users u ON u.id = c.granted_by
        WHERE c.vault_id = $1 AND c.user_id = $2
        ORDER BY c.source_key
        """,
        vault_id, user_id,
    )
    return [dict(r) for r in rows]


async def _materialize(conn, vault_id, user_id) -> str | None:
    """Recompute `vault_access` from the pair's contributions.

    Runs in the caller's transaction. `created_at` is deliberately left alone on
    conflict: it means "since when does this pair have any access at all", and
    letting it follow the winning contribution would move a member's `since`
    backwards whenever a contribution is removed.
    """
    rows = await conn.fetch(
        """
        SELECT role, granted_by
        FROM vault_access_contributions
        WHERE vault_id = $1 AND user_id = $2
        """,
        vault_id, user_id,
    )
    effective = effective_role(r["role"] for r in rows)
    if effective is None:
        await conn.execute(
            "DELETE FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            vault_id, user_id,
        )
        return None

    # Attribution follows the basis that won, which is the honest answer to
    # "who gave this person this role".
    granted_by = next(
        (r["granted_by"] for r in rows if r["role"] == effective), None,
    )
    await conn.execute(
        """
        INSERT INTO vault_access (id, vault_id, user_id, role, granted_by)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (vault_id, user_id)
        DO UPDATE SET role = $4, granted_by = $5
        """,
        uuid.uuid4(), vault_id, user_id, effective, granted_by,
    )
    return effective


async def apply_contribution(
    conn,
    vault_id,
    user_id,
    role: str,
    *,
    source_key: str = DIRECT_SOURCE_KEY,
    granted_by=None,
    revision: int | None = None,
) -> ContributionOutcome:
    """Record one basis for a pair and recompute the effective role.

    `revision` is what makes a retrying automated grantor safe: a write carrying
    a revision no newer than the stored one changes nothing. Callers that do not
    track a revision — every caller that existed before contributions did — pass
    None and get the next one.
    """
    if role not in CONTRIBUTION_ROLES:
        raise ValueError(
            f"invalid contribution role: {role!r}. Use: "
            + ", ".join(sorted(CONTRIBUTION_ROLES))
        )
    validate_source_key(source_key)

    await _lock_vault(conn, vault_id)
    previous_effective = await current_effective_role(conn, vault_id, user_id)
    stored = await conn.fetchrow(
        """
        SELECT role, revision FROM vault_access_contributions
        WHERE vault_id = $1 AND user_id = $2 AND source_key = $3
        """,
        vault_id, user_id, source_key,
    )

    if revision is None:
        next_revision = (stored["revision"] if stored else 0) + 1
    elif stored is not None and revision <= stored["revision"]:
        # Replay. Not an error: a grantor retrying after a lost response is
        # doing the right thing, and the stored state is already newer.
        return ContributionOutcome(
            applied=False,
            effective_role=previous_effective,
            previous_effective_role=previous_effective,
            contribution_role=stored["role"],
        )
    else:
        next_revision = revision

    await conn.execute(
        """
        INSERT INTO vault_access_contributions
            (id, vault_id, user_id, role, source_key, granted_by, revision)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (vault_id, user_id, source_key)
        DO UPDATE SET role = $4, granted_by = $6, revision = $7, updated_at = NOW()
        """,
        uuid.uuid4(), vault_id, user_id, role, source_key, granted_by, next_revision,
    )
    effective = await _materialize(conn, vault_id, user_id)
    return ContributionOutcome(
        applied=True,
        effective_role=effective,
        previous_effective_role=previous_effective,
        contribution_role=role,
    )


async def remove_contribution(
    conn,
    vault_id,
    user_id,
    *,
    source_key: str = DIRECT_SOURCE_KEY,
    revision: int | None = None,
) -> ContributionOutcome:
    """Remove one basis and recompute.

    If other bases remain the user is **downgraded**; only the last removal
    deletes the `vault_access` row. So a caller must not read a successful
    removal as "this user can no longer reach this vault" — `effective_role`
    on the outcome is the fact, and it may be unchanged.
    """
    validate_source_key(source_key)

    await _lock_vault(conn, vault_id)
    previous_effective = await current_effective_role(conn, vault_id, user_id)
    stored = await conn.fetchrow(
        """
        SELECT role, revision FROM vault_access_contributions
        WHERE vault_id = $1 AND user_id = $2 AND source_key = $3
        """,
        vault_id, user_id, source_key,
    )
    if stored is None:
        # Nothing to remove on this basis. The pair may still hold access on
        # another, so the effective role is reported rather than assumed gone.
        return ContributionOutcome(
            applied=False,
            effective_role=previous_effective,
            previous_effective_role=previous_effective,
            contribution_role=None,
        )
    if revision is not None and revision <= stored["revision"]:
        return ContributionOutcome(
            applied=False,
            effective_role=previous_effective,
            previous_effective_role=previous_effective,
            contribution_role=stored["role"],
        )

    await conn.execute(
        """
        DELETE FROM vault_access_contributions
        WHERE vault_id = $1 AND user_id = $2 AND source_key = $3
        """,
        vault_id, user_id, source_key,
    )
    effective = await _materialize(conn, vault_id, user_id)
    return ContributionOutcome(
        applied=True,
        effective_role=effective,
        previous_effective_role=previous_effective,
        contribution_role=None,
    )


async def remove_all_contributions(conn, vault_id, user_id) -> ContributionOutcome:
    """Remove every basis for a pair — what an administrator's revoke means.

    An explicit human revoke is not "withdraw my own contribution"; it is "this
    person should not be in this vault". Leaving an automated grantor's basis
    behind would make the revoke silently ineffective, and the grantors that
    write those bases treat their rule as a floor precisely so this decision
    stands.
    """
    await _lock_vault(conn, vault_id)
    previous_effective = await current_effective_role(conn, vault_id, user_id)
    removed = await conn.fetchval(
        """
        WITH deleted AS (
            DELETE FROM vault_access_contributions
            WHERE vault_id = $1 AND user_id = $2
            RETURNING 1
        )
        SELECT count(*) FROM deleted
        """,
        vault_id, user_id,
    )
    effective = await _materialize(conn, vault_id, user_id)
    return ContributionOutcome(
        applied=bool(removed) or previous_effective is not None,
        effective_role=effective,
        previous_effective_role=previous_effective,
        contribution_role=None,
    )
