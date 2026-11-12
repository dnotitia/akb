-- The highest id of each Vault's events that retention has deleted. An event
-- tail cursor below it never received that event and is refused with 410. A
-- cursor at or above it missed nothing, however far below the Vault's first
-- retained event it is: event ids are shared by every Vault, so other Vaults'
-- events advance them too.
CREATE TABLE IF NOT EXISTS event_retention_watermarks (
    vault_id UUID PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    deleted_through BIGINT NOT NULL CHECK (deleted_through >= 0)
);

-- Deletions before this migration were not recorded. A Vault that still has
-- events may have lost everything before its earliest retained one, so its
-- watermark starts just below that event: the 410 boundary stays where it
-- was. A Vault with no retained events gets no row; it had no boundary either.
INSERT INTO event_retention_watermarks (vault_id, deleted_through)
SELECT e.vault_id, MIN(e.id) - 1
  FROM events e
  JOIN vaults v ON v.id = e.vault_id
 GROUP BY e.vault_id
ON CONFLICT (vault_id) DO NOTHING;
