-- The starting marks, once: for each Vault with events at or below the
-- boundary migration 133 recorded, just below the earliest of them. A Vault
-- that already has a mark keeps it: only the trigger has written one since
-- the install, and it is exact. The boundary row is consumed in the same
-- statement, so running this again sets nothing. It holds no lock on events
-- that stops writes.
--
-- The marks are computed first, without locks. Then only the Vaults they
-- name are read FOR KEY SHARE, which keeps each from being deleted until
-- this commits: a delete of one under way is waited for, and a Vault it
-- removed is skipped, so no mark names a Vault that is gone. A Vault with
-- no mark to set is not locked, so a long transaction on it holds up
-- nothing here.
WITH pending AS (
    DELETE FROM event_retention_backfill
    RETURNING boundary
), earliest AS (
    SELECT e.vault_id, MIN(e.id) - 1 AS deleted_through
      FROM events e
     WHERE e.id <= (SELECT MAX(boundary) FROM pending)
       AND e.vault_id IS NOT NULL
     GROUP BY e.vault_id
), live AS (
    SELECT v.id
      FROM vaults v
     WHERE v.id IN (SELECT vault_id FROM earliest)
       FOR KEY SHARE
)
INSERT INTO event_retention_watermarks (vault_id, deleted_through)
SELECT earliest.vault_id, earliest.deleted_through
  FROM earliest
  JOIN live ON live.id = earliest.vault_id
ON CONFLICT (vault_id) DO NOTHING;
