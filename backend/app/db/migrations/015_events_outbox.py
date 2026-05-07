"""Migration 015: events outbox + LISTEN/NOTIFY trigger.

Domain change events (document put/update/delete, link/unlink, vault
grant, publish/unpublish, etc.) land in `events` in the same transaction
as the change itself — transactional outbox. A separate worker drains
the table to Redis Streams when configured; an in-process listener can
wake on `pg_notify('akb_events', id::text)` for SSE fanout without
polling.

PG is the source of truth: even if Redis is down for hours, no events
are lost — they accumulate in `events` and the publisher catches up
when Redis comes back. Same crash-safety pattern as
`vector_delete_outbox` (migration 005).

Idempotent.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import get_pool, init_db, close_pool

logger = logging.getLogger("akb.migration.015")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id              BIGSERIAL PRIMARY KEY,
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            vault_id        UUID,
            kind            TEXT NOT NULL,
            ref_type        TEXT,
            ref_id          TEXT,
            -- TEXT mirrors documents.created_by — `agent_id` flows in
            -- as a username string (not a UUID) on the MCP path.
            actor_id        TEXT,
            payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
            redis_published_at TIMESTAMPTZ,
            attempts        INTEGER NOT NULL DEFAULT 0,
            last_error      TEXT,
            next_attempt_at TIMESTAMPTZ
        )
        """
    )

    # Self-heal: an earlier revision of this migration created actor_id
    # as UUID. Switch to TEXT in place if so. Safe when actor_id is
    # already TEXT (no-op) or when the table was just created with
    # the new schema above.
    await conn.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'events' AND column_name = 'actor_id'
                   AND data_type = 'uuid'
            ) THEN
                ALTER TABLE events ALTER COLUMN actor_id TYPE TEXT USING actor_id::text;
            END IF;
        END $$;
        """
    )

    # Publisher claim path — partial index on the unpublished subset so
    # `WHERE redis_published_at IS NULL ORDER BY next_attempt_at` doesn't
    # degrade to a full scan as the table grows.
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_publish_pending
            ON events (next_attempt_at NULLS FIRST, id)
         WHERE redis_published_at IS NULL
        """
    )

    # Sweep path — purge published rows older than the grace window.
    # Mirrors `idx_vector_delete_processed` from migration 007.
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_published
            ON events (redis_published_at)
         WHERE redis_published_at IS NOT NULL
        """
    )

    # Per-vault tail (subscribers reading "give me events for vault X
    # since id Y"). Cheap enough to always have.
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_vault_id
            ON events (vault_id, id)
         WHERE vault_id IS NOT NULL
        """
    )

    # NOTIFY on insert. Payload is just the row id — full row stays in
    # PG. 8KB NOTIFY limit isn't a concern that way, and listeners pay
    # one roundtrip to fetch only the rows they care about.
    await conn.execute(
        """
        CREATE OR REPLACE FUNCTION akb_events_notify()
        RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify('akb_events', NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    # Drop-and-recreate so the trigger picks up function changes on
    # re-apply. The trigger is cheap (single PERFORM) so churn is fine.
    await conn.execute("DROP TRIGGER IF EXISTS akb_events_notify_trigger ON events")
    await conn.execute(
        """
        CREATE TRIGGER akb_events_notify_trigger
        AFTER INSERT ON events
        FOR EACH ROW EXECUTE FUNCTION akb_events_notify()
        """
    )

    logger.info("Migration 015 applied: events outbox + NOTIFY trigger")


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
