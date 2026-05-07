"""Migration 010: external git read-only mirror schema.

Adds three pieces, all additive:

1. `vault_external_git` — sidecar 1:1 extension of `vaults`. Holds the
   per-vault remote pointer (URL, branch, optional auth token) and the
   poller bookkeeping fields (`last_synced_sha`, retry/backoff). A vault
   without a row here is a normal "manual" vault — existing code is
   completely unaffected.

2. `documents` columns — `source` ('manual'|'external_git'),
   `external_path` (path within the upstream repo), `external_blob`
   (git blob sha, used as the content fingerprint by the reconciler),
   and `llm_metadata_at` (timestamp set by metadata_worker once it has
   filled missing summary/tags). All default-friendly so existing rows
   become well-formed `source='manual'` documents automatically.

3. `llm_metadata_cache` — keyed by git blob sha so that the same file
   appearing under different paths (or in multiple mirrors of the same
   repo) only triggers one LLM call. (Dropped in migration 012 — cache
   hit rate wasn't worth the extra table; kept here for migration
   history fidelity.)

Idempotent — safe to re-run on fresh and existing DBs.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.010")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # 1. Sidecar table for external git config + poller cursor.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_external_git (
            vault_id           UUID PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
            remote_url         TEXT NOT NULL,
            remote_branch      TEXT NOT NULL DEFAULT 'main',
            auth_token         TEXT,                              -- plaintext until Vault integration
            poll_interval_secs INTEGER NOT NULL DEFAULT 300,
            last_synced_sha    TEXT,
            last_synced_at     TIMESTAMPTZ,
            last_error         TEXT,
            retry_count        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vault_external_git_due
            ON vault_external_git (next_attempt_at)
        """
    )

    # 2. documents extension.
    # `llm_*` columns mirror the `embed_*` retry pattern from migration 004 —
    # metadata_worker uses them to back off transient LLM failures and
    # eventually abandon a row after MAX_RETRIES.
    await conn.execute(
        """
        ALTER TABLE documents
            ADD COLUMN IF NOT EXISTS source                TEXT NOT NULL DEFAULT 'manual',
            ADD COLUMN IF NOT EXISTS external_path         TEXT,
            ADD COLUMN IF NOT EXISTS external_blob         TEXT,
            ADD COLUMN IF NOT EXISTS llm_metadata_at       TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS llm_retry_count       INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS llm_last_error        TEXT,
            ADD COLUMN IF NOT EXISTS llm_next_attempt_at   TIMESTAMPTZ
        """
    )
    await conn.execute(
        """
        ALTER TABLE documents
            DROP CONSTRAINT IF EXISTS documents_source_check
        """
    )
    await conn.execute(
        """
        ALTER TABLE documents
            ADD CONSTRAINT documents_source_check
            CHECK (source IN ('manual','external_git'))
        """
    )

    # Partial indexes — only carry external rows, keep manual-only deployments
    # paying nothing.
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_external_path
            ON documents (vault_id, external_path)
         WHERE source = 'external_git'
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_external_blob
            ON documents (vault_id, external_blob)
         WHERE source = 'external_git'
        """
    )
    # metadata_worker drains rows here. Scoped to external docs so that
    # backfilling LLM summaries for legacy manual docs is an opt-in
    # decision later, not a side effect of this migration.
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_llm_pending
            ON documents (llm_next_attempt_at NULLS FIRST, id)
         WHERE source = 'external_git' AND llm_metadata_at IS NULL
        """
    )

    # 3. Per-blob LLM metadata cache. Key is git blob sha; value mirrors
    # the columns metadata_worker writes back to documents.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_metadata_cache (
            content_hash TEXT PRIMARY KEY,
            summary      TEXT,
            tags         TEXT[],
            doc_type     TEXT,
            domain       TEXT,
            model        TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    logger.info(
        "Migration 010 applied: vault_external_git, documents source/external_*/llm_metadata_at, llm_metadata_cache"
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
