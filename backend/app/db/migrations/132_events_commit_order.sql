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
-- otherwise hold a Vault's key and stall that Vault's writes. A Vault's row
-- is created by its first event.
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
                INSERT INTO public.vault_event_order (vault_id) VALUES (NEW.vault_id)
                    ON CONFLICT (vault_id) DO NOTHING;
                PERFORM 1 FROM public.vault_event_order
                 WHERE vault_id = NEW.vault_id
                   FOR UPDATE;
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
