-- The highest id of each Vault's events that has been deleted. An event tail
-- cursor below it never received that event and is refused with 410. A
-- cursor at or above it missed nothing, however far below the Vault's first
-- retained event it is: event ids are shared by every Vault, so other Vaults'
-- events advance them too.
CREATE TABLE IF NOT EXISTS event_retention_watermarks (
    vault_id UUID PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    deleted_through BIGINT NOT NULL CHECK (deleted_through >= 0)
);

-- The last event id handed out when the trigger below was installed.
-- Migration 134 sets the starting marks from the events at or below it, and
-- deletes the row as it does; every event above it was inserted after the
-- trigger, which records any deletion of it. One row at most.
CREATE TABLE IF NOT EXISTS event_retention_backfill (
    boundary BIGINT NOT NULL CHECK (boundary >= 0)
);

-- Every delete from events raises the mark of each Vault it touched, in the
-- deleting statement, whichever code issues it: this release's sweeper, an
-- earlier release's during a rolling deployment or after a rollback, or an
-- operator. Events of a Vault that no longer exists record nothing. The
-- Vaults a delete touched are read FOR KEY SHARE, which keeps each from
-- being deleted until the delete commits: a Vault delete under way is waited
-- for, and a Vault it removed records nothing. Read from the statement's
-- snapshot alone, such a Vault failed the mark's foreign key check, and the
-- whole delete with it, the sweep of every other Vault's events included.
CREATE OR REPLACE FUNCTION akb_events_record_deletions()
RETURNS TRIGGER AS $$
BEGIN
    WITH deleted AS (
        SELECT d.vault_id, MAX(d.id) AS deleted_through
          FROM akb_deleted_events d
         WHERE d.vault_id IS NOT NULL
         GROUP BY d.vault_id
    ), live AS (
        SELECT v.id
          FROM vaults v
         WHERE v.id IN (SELECT vault_id FROM deleted)
           FOR KEY SHARE
    )
    INSERT INTO event_retention_watermarks AS w (vault_id, deleted_through)
    SELECT deleted.vault_id, deleted.deleted_through
      FROM deleted
      JOIN live ON live.id = deleted.vault_id
    ON CONFLICT (vault_id) DO UPDATE
       SET deleted_through = GREATEST(w.deleted_through, EXCLUDED.deleted_through);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- The trigger and the backfill boundary are installed together, once, while
-- inserts and deletes on events wait. Once that lock is granted no write to
-- events is under way, so every committed event has an id the id sequence
-- has already handed out, and every later event draws a larger one: the
-- boundary is the sequence's last value, the one before its next value when
-- none has been drawn since it was set. Nothing here reads events, so the
-- lock lasts as long however many rows, live or deleted and not yet cleaned
-- up, the table holds. This holds whether the column's default or a trigger
-- draws the ids, as long as they come from the sequence the column owns. A
-- sequence cache above 1 would break it: each session draws a block of ids
-- ahead, and later hands out the rest of its block, ids at or below the
-- sequence's last value. So the cache is set to 1 here first, under the lock,
-- before the boundary is read. That rewrites the sequence, and every session
-- drops what is left of its block at its next draw and draws above the
-- boundary; none can draw before then, as no write to events can start. The
-- trigger's presence, checked again under the lock, records that this was
-- done, so running this again records no second boundary. The migration's
-- Python step checks for the trigger before it asks for any lock, and asks
-- with a short lock timeout and retries, so later event inserts do not queue
-- behind a waiting request.
DO $$
DECLARE
    id_sequence TEXT := pg_get_serial_sequence('events', 'id');
    id_cache BIGINT;
    last_drawn BIGINT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'events'::regclass AND tgname = 'akb_events_record_deletions'
    ) THEN
        LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE;
        IF id_sequence IS NULL THEN
            RAISE EXCEPTION 'events.id owns no sequence to take the backfill boundary from';
        END IF;
        SELECT seqcache INTO id_cache FROM pg_sequence WHERE seqrelid = id_sequence::regclass;
        IF id_cache <> 1 THEN
            EXECUTE format('ALTER SEQUENCE %s CACHE 1', id_sequence);
        END IF;
        EXECUTE format(
            'SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END FROM %s',
            id_sequence
        ) INTO last_drawn;
        INSERT INTO event_retention_backfill (boundary) VALUES (GREATEST(last_drawn, 0));
        CREATE TRIGGER akb_events_record_deletions
        AFTER DELETE ON events
        REFERENCING OLD TABLE AS akb_deleted_events
        FOR EACH STATEMENT EXECUTE FUNCTION akb_events_record_deletions();
    END IF;
END $$;
