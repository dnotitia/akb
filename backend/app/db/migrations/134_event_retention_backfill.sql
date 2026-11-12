-- The starting marks, once: for each Vault with events at or below the
-- boundary migration 133 recorded, just below the earliest of them. A Vault
-- that already has a mark keeps it: only the trigger has written one since
-- the install, and it is exact. The boundary row is consumed in the same
-- statement, so running this again sets nothing. It holds no lock on events
-- that stops writes.
WITH pending AS (
    DELETE FROM event_retention_backfill
    RETURNING boundary
)
INSERT INTO event_retention_watermarks (vault_id, deleted_through)
SELECT e.vault_id, MIN(e.id) - 1
  FROM events e
  JOIN vaults v ON v.id = e.vault_id
 WHERE e.id <= (SELECT MAX(boundary) FROM pending)
 GROUP BY e.vault_id
ON CONFLICT (vault_id) DO NOTHING;
