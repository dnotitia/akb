"""Migration 033: add `users.auth_provider` to tag external-IdP accounts.

AKB's baseline auth is local (username/password → bcrypt). The optional
Keycloak OIDC login path provisions a user row on first SSO login (JIT),
but such a user has no usable local password. This column records *how*
the account authenticates so the local login path can refuse a password
login against an SSO-only account instead of silently failing the bcrypt
compare.

Values:
- ``'local'``   — registered via POST /auth/register; has a real bcrypt hash.
- ``'keycloak'``— JIT-provisioned on first Keycloak login; ``password_hash``
                  holds an unusable sentinel that no bcrypt input can match.

Default ``'local'`` so every pre-migration row keeps its current behavior.
The column is NOT a hard auth switch: it is advisory metadata. Keycloak
itself is gated by ``keycloak_enabled`` in config — when that is false this
column is simply never read, and AKB behaves exactly as before.

Idempotent: ``ADD COLUMN IF NOT EXISTS`` so re-running is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.033")


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
           AND column_name = 'auth_provider'
        """
    )
    if existing:
        logger.info(
            "Migration 033: users.auth_provider already exists; skipping"
        )
        return

    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE users
              ADD COLUMN IF NOT EXISTS auth_provider TEXT NOT NULL DEFAULT 'local'
            """
        )

    logger.info(
        "Migration 033 added users.auth_provider (default 'local' — "
        "existing accounts unaffected; only SSO-provisioned rows differ)"
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
