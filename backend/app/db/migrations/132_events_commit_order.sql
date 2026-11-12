-- Within one Vault, assign `events.id` in commit order.
--
-- The column default draws the id when the row is inserted, but the row
-- becomes visible when its transaction commits. A cursor reader that saw a
-- later id first would move past an earlier id that committed after it. For an
-- event with a Vault, this trigger first takes a transaction-scoped advisory
-- lock for that Vault and then draws the id again. The lock is held until
-- commit or rollback, so the next event of the same Vault draws its id only
-- after every earlier one is visible or gone.
--
-- Column defaults are evaluated before BEFORE triggers, so a Vault event
-- consumes two sequence values and keeps the second. Events without a Vault
-- keep their default id and take no lock.
--
-- A sequence cache larger than 1 hands each session its own block of values,
-- so a session could draw a smaller id after another session's larger one.
-- The migration sets the cache to 1 when it is not.
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
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended('akb-events:' || NEW.vault_id::text, 0)
                );
                NEW.id := pg_catalog.nextval(%L::regclass);
            END IF;
            RETURN NEW;
        END
        $function$
        $create$,
        sequence_name
    );
END
$migration$;

DROP TRIGGER IF EXISTS akb_events_commit_order_trigger ON public.events;
CREATE TRIGGER akb_events_commit_order_trigger
    BEFORE INSERT ON public.events
    FOR EACH ROW EXECUTE FUNCTION public.akb_events_commit_order();
