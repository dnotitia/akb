"""Migration 034: `oidc_transients` — short-lived OIDC flow state.

The optional Keycloak login flow has two short-lived secrets that must
survive the redirect round-trip AND work when AKB runs more than one
backend replica (the login redirect and the callback can land on
different pods, so in-process dicts would lose the state):

- ``kind='state'``    — CSRF state token + (for PKCE clients) the
                        code_verifier + the post-login redirect path.
                        Issued by /auth/keycloak/login, consumed by the
                        callback.
- ``kind='exchange'`` — a one-time opaque code mapped to the freshly
                        minted AKB JWT + user payload. Issued by the
                        callback, consumed by /auth/keycloak/exchange so
                        the token is delivered over a POST body instead
                        of riding in a redirect URL.

Both are single-use (consumed via DELETE … RETURNING) and TTL-bounded
(``expires_at``). The table is harmless when Keycloak is disabled — it
simply stays empty.

Idempotent: ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.034")


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
            CREATE TABLE IF NOT EXISTS oidc_transients (
                key         TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
                expires_at  TIMESTAMPTZ NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_oidc_transients_expiry "
            "ON oidc_transients(expires_at)"
        )

    logger.info("Migration 034 ensured oidc_transients table")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
