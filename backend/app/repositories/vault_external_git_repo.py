"""Repository for vault_external_git operations.

Sidecar 1:1 to `vaults` — only present for vaults that mirror an
external git repo. All access guards, polling, and reconcile bookkeeping
go through here so the rest of the codebase doesn't grow ad-hoc SQL
against this table.
"""

from __future__ import annotations

import uuid

import asyncpg


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

    async def mark_success(
        self,
        vault_id: uuid.UUID,
        poll_interval_secs: int,
        new_sha: str | None = None,
    ) -> None:
        """Successful reconcile or no-op (unchanged HEAD). When `new_sha`
        is provided the cursor advances; when None (remote HEAD matched
        existing cursor) only the next-poll schedule resets.
        """
        async with self.pool.acquire() as conn:
            if new_sha is None:
                await conn.execute(
                    """
                    UPDATE vault_external_git
                       SET last_error      = NULL,
                           retry_count     = 0,
                           next_attempt_at = NOW() + ($2 || ' seconds')::interval,
                           updated_at      = NOW()
                     WHERE vault_id = $1
                    """,
                    vault_id, str(poll_interval_secs),
                )
            else:
                await conn.execute(
                    """
                    UPDATE vault_external_git
                       SET last_synced_sha    = $2,
                           last_synced_at     = NOW(),
                           last_error         = NULL,
                           retry_count        = 0,
                           next_attempt_at    = NOW() + ($3 || ' seconds')::interval,
                           updated_at         = NOW()
                     WHERE vault_id = $1
                    """,
                    vault_id, new_sha, str(poll_interval_secs),
                )

    async def mark_failure(
        self,
        vault_id: uuid.UUID,
        error: str,
        backoff_secs: int,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vault_external_git
                   SET last_error      = $2,
                       retry_count     = retry_count + 1,
                       next_attempt_at = NOW() + ($3 || ' seconds')::interval,
                       updated_at      = NOW()
                 WHERE vault_id = $1
                """,
                vault_id, error[:500], str(backoff_secs),
            )
