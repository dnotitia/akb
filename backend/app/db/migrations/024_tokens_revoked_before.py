"""Migration 024: add `users.tokens_revoked_before` for JWT revocation.

JWTs are stateless — the server cannot recall an already-issued token
without keeping per-user state. Before this column, the only way to
invalidate a leaked/stale JWT was to rotate ``jwt_secret`` (which kills
every session in the system).

The column holds the timestamp after which any JWT issued by this user
is rejected on resolve. ``resolve_token`` compares the JWT's ``iat``
claim against this column: ``iat < tokens_revoked_before`` → 401.

Default ``epoch`` (1970-01-01) so every pre-migration JWT continues to
work (every plausible ``iat`` is strictly greater than the default).

Triggering points (set the column to ``NOW()``):
- ``POST /api/v1/auth/revoke-all-sessions`` — caller voluntarily ends
  every session they have (other devices, mobile clients, agents).
- ``POST /api/v1/admin/users/{id}/revoke-sessions`` — admin force-logout
  of a user (incident response, credential rotation, employee offboard).
- ``PATCH /api/v1/auth/password`` (change password) — auto-revoke so
  the password change actually invalidates old sessions.

Idempotent: ``ADD COLUMN IF NOT EXISTS`` so re-running is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.024")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    existing = await conn.fetchval(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'users'
           AND column_name = 'tokens_revoked_before'
        """
    )
    if existing:
        logger.info(
            "Migration 024: users.tokens_revoked_before already exists; skipping"
        )
        return

    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE users
              ADD COLUMN IF NOT EXISTS tokens_revoked_before TIMESTAMPTZ
                NOT NULL DEFAULT TIMESTAMPTZ '1970-01-01 00:00:00+00'
            """
        )

    logger.info(
        "Migration 024 added users.tokens_revoked_before (default epoch — "
        "existing JWTs unaffected until the user voluntarily revokes)"
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
