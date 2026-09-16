"""Let a bridged revision name its body by digest instead of only by git."""

from __future__ import annotations

SCHEMA = """
-- A bridged revision's body lives in the git working store, reachable only
-- from a pod that mounts it. That pins the serving tier to one volume and
-- leaves a third of all revision bodies outside the database that is
-- otherwise the authority.
--
-- This column is how a mapping stops needing git: once it names a digest,
-- the body is read from the payload store instead. Nullable on purpose —
-- the two sources coexist, one mapping at a time, and git stays the fallback
-- for every row that has not moved. Setting it back to NULL is the rollback.
ALTER TABLE legacy_revision_mappings
    ADD COLUMN IF NOT EXISTS body_digest TEXT;

DO $$
BEGIN
    ALTER TABLE legacy_revision_mappings
        ADD CONSTRAINT legacy_revision_mappings_body_digest_shape
        CHECK (body_digest IS NULL OR body_digest ~ '^[0-9a-f]{64}$');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- The backfill asks one question repeatedly: which bridged mappings still
-- have no digest. A partial index answers it without scanning the migrated
-- majority, and shrinks to nothing as the work finishes.
--
-- All four columns, in this order, because the backfill pages through them
-- by keyset: the first three are not unique by any constraint (the primary
-- key is (resource_id, legacy_git_oid)), so a cursor built on them alone
-- could step over a row.
CREATE INDEX IF NOT EXISTS idx_legacy_revision_mappings_unmigrated_body
    ON legacy_revision_mappings
       (namespace_id, resource_id, lineage_ordinal, legacy_git_oid)
    WHERE resolution = 'bridge' AND body_digest IS NULL;
"""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA)
    else:
        await conn.execute(SCHEMA)
