-- Name the event's Vault in the notification: `<vault_id>:<id>`, with an empty
-- Vault part for an event that has none. The trigger from migration 015 calls
-- this function and stays as it is.
CREATE OR REPLACE FUNCTION akb_events_notify()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('akb_events', COALESCE(NEW.vault_id::text, '') || ':' || NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
