-- The starting marks, once: for each Vault with events at or below the
-- boundary migration 133 recorded, just below the earliest of them. A Vault
-- that already has a mark keeps it: only the trigger has written one since
-- the install, and it is exact. The boundary row is consumed in the same
-- statement, so running this again sets nothing. It holds no lock on events
-- that stops writes.
--
-- The Vaults are read FOR KEY SHARE, which keeps each one from being deleted
-- until this commits: a Vault delete under way is waited for, and a Vault it
-- removed is skipped, so no mark names a Vault that is gone. Read from this
-- statement's snapshot alone, such a Vault failed the mark's foreign key
-- check, and the migration with it.
WITH pending AS (
    DELETE FROM event_retention_backfill
    RETURNING boundary
), live AS (
    SELECT id FROM vaults FOR KEY SHARE
)
INSERT INTO event_retention_watermarks (vault_id, deleted_through)
SELECT e.vault_id, MIN(e.id) - 1
  FROM events e
  JOIN live v ON v.id = e.vault_id
 WHERE e.id <= (SELECT MAX(boundary) FROM pending)
 GROUP BY e.vault_id
ON CONFLICT (vault_id) DO NOTHING;
