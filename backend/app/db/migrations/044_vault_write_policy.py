"""Migration 044: vault-grain write-policy tables (P0 slice S3 substrate).

Adds two additive sidecar tables — no changes to any existing table.
Generalizes the migration-010 ``vault_external_git`` sidecar pattern to a
new axis: a vault MAY be marked so that mutating calls are only accepted
from PATs on an explicit per-vault grant list (token-allowlist,
class-agnostic — a collector PAT, a gardener PAT, an operator PAT, etc.
are all just rows in ``vault_write_grants``. See
plans/2026-07-10-naut-edit-semantics-sot-writeback-design.md §5.1a).

1. ``vault_write_policy`` — 1:1 marker. A vault WITHOUT a row here is
   ungoverned (existing behaviour, unaffected). ``managed_by`` is a
   free-text provenance label (e.g. 'collector:<binding>',
   'gardener:<policy>').

2. ``vault_write_grants`` — the allowlist itself. ``(vault_id, token_id)``
   composite PK; a grant can only exist for a vault that already has a
   ``vault_write_policy`` row (FK to its PK, not to ``vaults`` directly).

   IMPORTANT — token deletion silently drops the grant (``token_id ON
   DELETE CASCADE``). There is no independent record that a grant ever
   existed once its token is gone. Any token rotation against a marked
   vault MUST grant the new token BEFORE revoking the old one
   (grant-new-before-revoke-old) — revoking first, even for an instant,
   leaves the vault with zero live writers. See
   ``app/repositories/vault_write_policy_repo.py``.

This slice (P0 S3) builds ONLY the substrate: the two tables + the repo.
Nothing reads them yet — no guard, no API route (that is a later slice).
Marking a vault today has zero runtime effect.

Idempotent — safe to re-run on fresh and existing DBs (mirrors migration
010/042's ``CREATE TABLE IF NOT EXISTS`` + transaction-wrapped style).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.044")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vault_write_policy (
                vault_id UUID PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
                managed_by TEXT NOT NULL,
                note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_by TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vault_write_grants (
                vault_id UUID NOT NULL REFERENCES vault_write_policy(vault_id) ON DELETE CASCADE,
                token_id UUID NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
                granted_by TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (vault_id, token_id)
            )
            """
        )
        # A token revocation cascade-deletes through this FK; without an
        # index on the referencing column that cascade sequential-scans
        # the whole grants table on every token delete (suspend/revoke/
        # rotation, all elsewhere in the codebase already).
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vault_write_grants_token
                ON vault_write_grants(token_id)
            """
        )

    logger.info("Migration 044 added vault_write_policy + vault_write_grants")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
