-- The highest id of each Vault's events that has been deleted. An event tail
-- cursor below it never received that event and is refused with 410. A
-- cursor at or above it missed nothing, however far below the Vault's first
-- retained event it is: event ids are shared by every Vault, so other Vaults'
-- events advance them too.
CREATE TABLE IF NOT EXISTS event_retention_watermarks (
    vault_id UUID PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    deleted_through BIGINT NOT NULL CHECK (deleted_through >= 0)
);

-- Every delete from events raises the mark of each Vault it touched, in the
-- deleting statement, whichever code issues it: this release's sweeper, an
-- earlier release's during a rolling deployment or after a rollback, or an
-- operator. Events of a Vault that no longer exists record nothing.
CREATE OR REPLACE FUNCTION akb_events_record_deletions()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO event_retention_watermarks AS w (vault_id, deleted_through)
    SELECT d.vault_id, MAX(d.id)
      FROM akb_deleted_events d
      JOIN vaults v ON v.id = d.vault_id
     GROUP BY d.vault_id
    ON CONFLICT (vault_id) DO UPDATE
       SET deleted_through = GREATEST(w.deleted_through, EXCLUDED.deleted_through);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- The starting marks and the trigger are installed together, once, while
-- deletes and inserts on events wait: no delete can fall between the two, and
-- the trigger's presence records that the starting marks were set, so
-- running this again sets none (a mark set later would claim a deletion that
-- never happened). Deletions before this point left no record, and cannot be
-- reconstructed. A Vault that still has events starts just below its earliest
-- one, the boundary the tail used until now, so history that may already have
-- been swept is still refused; a later event deleted while an earlier one was
-- kept stays unknown, as it was before. A Vault with no events starts without
-- a mark, as it had no boundary before. Reads are not blocked. The migration's
-- Python step takes this lock before running this file, with a short lock
-- timeout and retries, so later event inserts do not queue behind a waiting
-- request; once held, it lasts as long as one grouped scan of events, which
-- retention keeps small.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'events'::regclass AND tgname = 'akb_events_record_deletions'
    ) THEN
        LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE;
        INSERT INTO event_retention_watermarks (vault_id, deleted_through)
        SELECT e.vault_id, MIN(e.id) - 1
          FROM events e
          JOIN vaults v ON v.id = e.vault_id
         GROUP BY e.vault_id
        ON CONFLICT (vault_id) DO NOTHING;
        CREATE TRIGGER akb_events_record_deletions
        AFTER DELETE ON events
        REFERENCING OLD TABLE AS akb_deleted_events
        FOR EACH STATEMENT EXECUTE FUNCTION akb_events_record_deletions();
    END IF;
END $$;
