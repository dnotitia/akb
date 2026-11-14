-- Within one Vault, assign `events.id` in commit order.
--
-- An id drawn when the row is inserted becomes visible only when its
-- transaction commits. A cursor reader that saw a later id first would move
-- past an earlier id that committed after it. For an event with a Vault, this
-- trigger first locks that Vault's row in `vault_event_order` and only then
-- draws the id. The row lock is held until commit or rollback, so the next
-- event of the same Vault draws its id only after every earlier one is
-- visible or gone. Events without a Vault take no lock.
--
-- The lock is a row lock on a table that no SQL role is granted, rather than
-- an advisory lock: any role may take any advisory lock, so a statement could
-- otherwise hold a Vault's key and stall that Vault's writes. Every Vault has
-- its row from the start: a trigger on `vaults` inserts the row with the Vault
-- and deletes it with the Vault, and the migration inserts the rows of the
-- Vaults that exist. An event of a Vault that has no row, such as one deleted
-- meanwhile, inserts the row.
--
-- A transaction waits at most 5 seconds in total for a Vault's lock.
-- `akb_lock_vault_events` polls the row with `FOR UPDATE SKIP LOCKED` against
-- one deadline. When there is no row it inserts one, waiting at most 20
-- milliseconds at a time for a concurrent inserter and checking the deadline
-- between attempts: `lock_timeout` applies to each lock wait on its own, and
-- one statement can wait more than once. A lock taken after the deadline
-- counts as not taken. Past the deadline the function fails with SQLSTATE
-- 55P03 and the constraint name `akb_event_lock_timeout`; that error alone is
-- reported by the API as a retryable `write_busy`, and its transaction rolls
-- back. Polling gives up first-come order among waiters, and a lock cycle
-- through a Vault's row ends at the deadline rather than by deadlock detection.
-- A Bare Git write calls the function before it commits to Git, so the lock is
-- held before the commit; the trigger then finds it already held.
--
-- The trigger draws every id. The column keeps no default, because defaults
-- are evaluated before BEFORE triggers and a default would spend a second
-- value on every event; the sequence stays owned by the column. An INSERT
-- that names no id, as earlier releases do, still gets the trigger's id.
--
-- A sequence cache larger than 1 hands each session its own block of values,
-- so a session could draw a smaller id after another session's larger one.
-- The migration sets the cache to 1 when it is not.
CREATE TABLE IF NOT EXISTS public.vault_event_order (
    vault_id UUID PRIMARY KEY
);

CREATE OR REPLACE FUNCTION public.akb_vault_event_order_row()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.vault_event_order (vault_id) VALUES (NEW.id)
            ON CONFLICT (vault_id) DO NOTHING;
    ELSE
        DELETE FROM public.vault_event_order WHERE vault_id = OLD.id;
    END IF;
    RETURN NULL;
END
$function$;

DROP TRIGGER IF EXISTS akb_vault_event_order_trigger ON public.vaults;
CREATE TRIGGER akb_vault_event_order_trigger
    AFTER INSERT OR DELETE ON public.vaults
    FOR EACH ROW EXECUTE FUNCTION public.akb_vault_event_order_row();

-- After the trigger: creating it waits for Vault inserts in progress and holds
-- new ones off until the migration commits, so this reads every Vault.
INSERT INTO public.vault_event_order (vault_id)
SELECT id FROM public.vaults
ON CONFLICT (vault_id) DO NOTHING;

CREATE OR REPLACE FUNCTION public.akb_lock_vault_events(target UUID)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public
-- The insert below shortens it per attempt; this clause restores the
-- caller's value when the function returns.
SET lock_timeout = '5s'
AS $function$
DECLARE
    deadline timestamptz := clock_timestamp() + interval '5 seconds';
    pause float8 := 0.002;
    acquired boolean;
BEGIN
    LOOP
        -- A row this transaction already locked or inserted is not skipped.
        PERFORM 1 FROM public.vault_event_order
         WHERE vault_id = target
           FOR UPDATE SKIP LOCKED;
        acquired := FOUND;
        EXIT WHEN acquired OR clock_timestamp() >= deadline;
        IF EXISTS (SELECT 1 FROM public.vault_event_order WHERE vault_id = target) THEN
            PERFORM pg_sleep(pause);
            pause := least(pause * 2, 0.02);
        ELSE
            -- No committed row: insert it. Another transaction that inserted it
            -- and has not ended is waited for at most 20 ms per attempt.
            PERFORM set_config(
                'lock_timeout',
                greatest(1, least(20, ceil(extract(epoch FROM deadline - clock_timestamp()) * 1000)))::int || 'ms',
                true
            );
            BEGIN
                INSERT INTO public.vault_event_order (vault_id) VALUES (target)
                    ON CONFLICT (vault_id) DO NOTHING;
            EXCEPTION WHEN lock_not_available THEN
                NULL;
            END;
        END IF;
    END LOOP;
    IF NOT acquired OR clock_timestamp() > deadline THEN
        RAISE EXCEPTION 'events of Vault % have been locked by another transaction for 5 seconds', target
            USING ERRCODE = 'lock_not_available', CONSTRAINT = 'akb_event_lock_timeout';
    END IF;
END
$function$;

DO $migration$
DECLARE
    sequence_name text;
    sequence_cache bigint;
BEGIN
    SELECT pg_catalog.format('%I.%I', n.nspname, c.relname), s.seqcache
      INTO sequence_name, sequence_cache
      FROM pg_catalog.pg_class AS c
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
      JOIN pg_catalog.pg_sequence AS s ON s.seqrelid = c.oid
     WHERE c.oid = pg_catalog.pg_get_serial_sequence('public.events', 'id')::regclass;
    IF sequence_name IS NULL THEN
        RAISE EXCEPTION 'events.id has no owned sequence';
    END IF;
    IF sequence_cache <> 1 THEN
        EXECUTE pg_catalog.format('ALTER SEQUENCE %s CACHE 1', sequence_name);
    END IF;
    EXECUTE pg_catalog.format(
        $create$
        CREATE OR REPLACE FUNCTION public.akb_events_commit_order()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF NEW.vault_id IS NOT NULL THEN
                PERFORM public.akb_lock_vault_events(NEW.vault_id);
            END IF;
            NEW.id := pg_catalog.nextval(%L::regclass);
            RETURN NEW;
        END
        $function$
        $create$,
        sequence_name
    );
END
$migration$;

ALTER TABLE public.events ALTER COLUMN id DROP DEFAULT;

DROP TRIGGER IF EXISTS akb_events_commit_order_trigger ON public.events;
CREATE TRIGGER akb_events_commit_order_trigger
    BEFORE INSERT ON public.events
    FOR EACH ROW EXECUTE FUNCTION public.akb_events_commit_order();

-- Row changes of dynamic tables are recorded when their transaction commits.
-- The statement-level trigger from migration 086 used to insert its
-- `table.rows_changed` event as each statement ended. A cascading delete
-- fires the parent statement's trigger before the next cascade level runs,
-- so the transaction then took the Vault lock above while it still waited
-- for grandchild rows, and a writer holding such a row and then inserting
-- its own event deadlocked with it. The trigger now queues the change, and a
-- deferred constraint trigger inserts the event at commit, after every lock
-- the transaction takes. Event content is unchanged: one event per
-- statement that changed rows, with the same Vault, URI, actor and payload.
CREATE TABLE IF NOT EXISTS public.events_row_change_queue (
    seq BIGSERIAL PRIMARY KEY,
    vault_id UUID NOT NULL,
    resource_uri TEXT NOT NULL,
    actor_id TEXT,
    operation TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION public.akb_events_row_change_flush()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    DELETE FROM public.events_row_change_queue WHERE seq = NEW.seq;
    INSERT INTO public.events (vault_id, kind, resource_uri, actor_id, payload)
    VALUES (
        NEW.vault_id,
        'table.rows_changed',
        NEW.resource_uri,
        NEW.actor_id,
        jsonb_build_object('operation', NEW.operation)
    );
    RETURN NULL;
END
$function$;

DROP TRIGGER IF EXISTS akb_events_row_change_flush_trigger ON public.events_row_change_queue;
CREATE CONSTRAINT TRIGGER akb_events_row_change_flush_trigger
    AFTER INSERT ON public.events_row_change_queue
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.akb_events_row_change_flush();

CREATE OR REPLACE FUNCTION public.akb_dynamic_table_rows_changed()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    actor TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF NOT EXISTS (SELECT 1 FROM akb_rows_changed_old) THEN
            RETURN NULL;
        END IF;
    ELSIF TG_OP IN ('INSERT', 'UPDATE') THEN
        IF NOT EXISTS (SELECT 1 FROM akb_rows_changed_new) THEN
            RETURN NULL;
        END IF;
    ELSE
        RAISE EXCEPTION 'unknown dynamic table row-change operation: %', TG_OP;
    END IF;

    actor := NULLIF(current_setting('akb.actor_id', true), '');
    INSERT INTO public.events_row_change_queue (vault_id, resource_uri, actor_id, operation)
    VALUES (TG_ARGV[0]::uuid, TG_ARGV[1], actor, lower(TG_OP));
    RETURN NULL;
END;
$function$;
