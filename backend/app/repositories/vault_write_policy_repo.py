"""Repository for vault_write_policy / vault_write_grants operations.

Sidecar 1:1 to ``vaults`` — generalizes the ``vault_external_git`` sidecar
pattern (migration 010) to a new axis. A vault WITHOUT a
``vault_write_policy`` row is ungoverned; existing code is completely
unaffected. A vault WITH a row is *marked*: mutating calls should only be
accepted from a PAT on its ``vault_write_grants`` allowlist
(class-agnostic — collector, gardener, and operator PATs are all just rows
here). This module is pure substrate: no caller enforces the allowlist
yet — that guard is a later slice (P0 S4). Marking a vault today has zero
runtime effect.

CASCADE HAZARD: ``vault_write_grants.token_id`` is ``ON DELETE CASCADE``
against ``tokens.id``. Deleting a token (revoke, suspend, rotation)
silently drops every grant row for it — there is no tombstone. Once the
guard lands, a vault left with zero live grants fail-closed rejects every
mutating call, so rotating a granted token MUST ``add_grant`` the new
token BEFORE ``remove_grant``-ing (or otherwise deleting) the old one —
grant-new-before-revoke-old. Revoking first, even for an instant, wedges
the vault with no live writer.
"""

from __future__ import annotations

import uuid

from app.db.postgres import get_pool

_GET_POLICY_SQL = """
    SELECT vault_id, managed_by, note, created_at, created_by
      FROM vault_write_policy
     WHERE vault_id = $1
"""

_IS_GRANTED_SQL = """
    SELECT 1 FROM vault_write_grants WHERE vault_id = $1 AND token_id = $2
"""

_SET_POLICY_SQL = """
    INSERT INTO vault_write_policy (vault_id, managed_by, note, created_by)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (vault_id) DO UPDATE
       SET managed_by = EXCLUDED.managed_by,
           note = EXCLUDED.note
    RETURNING vault_id, managed_by, note, created_at, created_by
"""

_REMOVE_POLICY_SQL = "DELETE FROM vault_write_policy WHERE vault_id = $1"

_ADD_GRANT_SQL = """
    INSERT INTO vault_write_grants (vault_id, token_id, granted_by)
    VALUES ($1, $2, $3)
    ON CONFLICT (vault_id, token_id) DO NOTHING
"""

_REMOVE_GRANT_SQL = (
    "DELETE FROM vault_write_grants WHERE vault_id = $1 AND token_id = $2"
)


async def get_policy(vault_id: uuid.UUID, conn=None) -> dict | None:
    """Return the policy row for ``vault_id``, or ``None`` if ungoverned."""
    if conn is not None:
        row = await conn.fetchrow(_GET_POLICY_SQL, vault_id)
    else:
        pool = await get_pool()
        async with pool.acquire() as acq:
            row = await acq.fetchrow(_GET_POLICY_SQL, vault_id)
    return dict(row) if row is not None else None


async def is_granted(vault_id: uuid.UUID, token_id: uuid.UUID, conn=None) -> bool:
    """True iff ``token_id`` is on ``vault_id``'s write-grant allowlist.

    This function alone does not tell a caller whether a write should be
    allowed — an ungoverned vault (no ``vault_write_policy`` row) has no
    allowlist to check against, and "no policy" means unrestricted, not
    deny. That branch belongs to the guard (a later slice), not here.
    """
    if conn is not None:
        found = await conn.fetchval(_IS_GRANTED_SQL, vault_id, token_id)
    else:
        pool = await get_pool()
        async with pool.acquire() as acq:
            found = await acq.fetchval(_IS_GRANTED_SQL, vault_id, token_id)
    return found is not None


async def set_policy(
    vault_id: uuid.UUID,
    managed_by: str,
    created_by: str,
    note: str | None = None,
    conn=None,
) -> dict:
    """Create or update the marking on ``vault_id`` (upsert on the PK).

    Re-marking an already-marked vault updates ``managed_by``/``note`` in
    place; ``created_at``/``created_by`` stay pinned to the original mark.
    """
    if conn is not None:
        row = await conn.fetchrow(_SET_POLICY_SQL, vault_id, managed_by, note, created_by)
    else:
        pool = await get_pool()
        async with pool.acquire() as acq:
            row = await acq.fetchrow(
                _SET_POLICY_SQL, vault_id, managed_by, note, created_by
            )
    return dict(row)


async def remove_policy(vault_id: uuid.UUID, conn=None) -> None:
    """Unmark ``vault_id``. Cascades away every grant row for it too."""
    if conn is not None:
        await conn.execute(_REMOVE_POLICY_SQL, vault_id)
    else:
        pool = await get_pool()
        async with pool.acquire() as acq:
            await acq.execute(_REMOVE_POLICY_SQL, vault_id)


async def add_grant(
    vault_id: uuid.UUID, token_id: uuid.UUID, granted_by: str, conn=None
) -> None:
    """Grant ``token_id`` write access to ``vault_id``.

    Requires an existing ``vault_write_policy`` row for ``vault_id`` (FK
    — call ``set_policy`` first, or this raises a foreign-key violation).
    Idempotent: granting an already-granted token is a no-op, not an error.
    """
    if conn is not None:
        await conn.execute(_ADD_GRANT_SQL, vault_id, token_id, granted_by)
    else:
        pool = await get_pool()
        async with pool.acquire() as acq:
            await acq.execute(_ADD_GRANT_SQL, vault_id, token_id, granted_by)


async def remove_grant(vault_id: uuid.UUID, token_id: uuid.UUID, conn=None) -> None:
    """Revoke ``token_id``'s write access to ``vault_id``.

    ROTATION SAFETY: ``add_grant`` the replacement token before calling
    this for the outgoing one — see the module docstring's cascade hazard.
    """
    if conn is not None:
        await conn.execute(_REMOVE_GRANT_SQL, vault_id, token_id)
    else:
        pool = await get_pool()
        async with pool.acquire() as acq:
            await acq.execute(_REMOVE_GRANT_SQL, vault_id, token_id)
