"""Migration 043: additive account state and stable external identities.

Existing users keep their IDs, password/auth-provider values, tokens, vaults,
and PostgreSQL roles. Compatibility defaults classify every existing row as an
active human; old application builds can keep using their existing INSERTs.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db


logger = logging.getLogger("akb.migration.043")


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
            ALTER TABLE users
              ADD COLUMN IF NOT EXISTS account_status TEXT NOT NULL DEFAULT 'active',
              ADD COLUMN IF NOT EXISTS account_kind TEXT NOT NULL DEFAULT 'human'
            """
        )
        await conn.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'users_account_status_check'
                   AND conrelid = 'users'::regclass
              ) THEN
                ALTER TABLE users
                  ADD CONSTRAINT users_account_status_check
                  CHECK (account_status IN ('active', 'suspended'));
              END IF;
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'users_account_kind_check'
                   AND conrelid = 'users'::regclass
              ) THEN
                ALTER TABLE users
                  ADD CONSTRAINT users_account_kind_check
                  CHECK (account_kind IN ('human', 'service'));
              END IF;
            END $$;
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_identities (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                issuer TEXT NOT NULL,
                subject TEXT NOT NULL,
                email_snapshot TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT external_identities_issuer_subject_key
                    UNIQUE (issuer, subject)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_external_identities_user
                ON external_identities(user_id)
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_token_cleanup (
                token_id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_account_token_cleanup_pending
                ON account_token_cleanup(user_id, requested_at)
                WHERE completed_at IS NULL
            """
        )

    logger.info(
        "Migration 043 added account state, external identities, and token cleanup ledger"
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
