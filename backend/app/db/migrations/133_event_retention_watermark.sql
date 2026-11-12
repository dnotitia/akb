-- The highest id of each Vault's events that has been deleted. An event tail
-- cursor below it never received that event and is refused with 410. A
-- cursor at or above it missed nothing, however far below the Vault's first
-- retained event it is: event ids are shared by every Vault, so other Vaults'
-- events advance them too.
CREATE TABLE IF NOT EXISTS event_retention_watermarks (
    vault_id UUID PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    deleted_through BIGINT NOT NULL CHECK (deleted_through >= 0)
);

-- The highest event id when the trigger below was installed. Migration 134
-- sets the starting marks from the events at or below it, and deletes the
-- row as it does; every event above it was inserted after the trigger, which
-- records any deletion of it. One row at most.
CREATE TABLE IF NOT EXISTS event_retention_backfill (
    boundary BIGINT NOT NULL CHECK (boundary >= 0)
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

-- The trigger and the backfill boundary are installed together, once, while
-- inserts and deletes on events wait: every event at or below the boundary
-- was committed before the trigger existed, and every later one after it.
-- Nothing here scans events: the boundary is read from the end of the
-- primary key, so the lock lasts no longer on a large table. The trigger's
-- presence, checked again under the lock, records that this was done, so
-- running this again records no second boundary. The migration's Python step
-- checks for the trigger before it asks for any lock, and asks with a short
-- lock timeout and retries, so later event inserts do not queue behind a
-- waiting request.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'events'::regclass AND tgname = 'akb_events_record_deletions'
    ) THEN
        LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE;
        INSERT INTO event_retention_backfill (boundary)
        SELECT COALESCE(MAX(id), 0) FROM events;
        CREATE TRIGGER akb_events_record_deletions
        AFTER DELETE ON events
        REFERENCING OLD TABLE AS akb_deleted_events
        FOR EACH STATEMENT EXECUTE FUNCTION akb_events_record_deletions();
    END IF;
END $$;
