"""Retain adoption evidence independently of deletable Vaults/installations.

The immutable UUIDs describe the original operation, not live ownership.
Validate and lock new references at write time without cascading, rewriting,
or permanently pinning their parents. Plan/target/audit retention guards and
all other foreign keys remain in force.
"""

from __future__ import annotations


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    if not await conn.fetchval(
        """
        SELECT bool_and(to_regclass('public.' || table_name) IS NOT NULL)
          FROM unnest($1::text[]) AS required(table_name)
        """,
        ["app_legacy_adoption_targets", "app_legacy_adoption_audit"],
    ):
        return
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE app_legacy_adoption_targets
                DROP CONSTRAINT IF EXISTS app_legacy_adoption_targets_vault_fkey,
                DROP CONSTRAINT IF EXISTS app_legacy_adoption_targets_installation_id_fkey,
                DROP CONSTRAINT IF EXISTS app_legacy_adoption_targets_installation_vault_fkey;
            ALTER TABLE app_legacy_adoption_audit
                DROP CONSTRAINT IF EXISTS app_legacy_adoption_audit_vault_fkey,
                DROP CONSTRAINT IF EXISTS app_legacy_adoption_audit_installation_id_fkey,
                DROP CONSTRAINT IF EXISTS app_legacy_adoption_audit_installation_vault_fkey;

            CREATE OR REPLACE FUNCTION akb_validate_legacy_adoption_live_references()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                installation_vault UUID;
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    IF NEW.vault_id IS NOT DISTINCT FROM OLD.vault_id
                       AND NEW.installation_id IS NOT DISTINCT FROM OLD.installation_id
                    THEN
                        RETURN NEW;
                    END IF;
                END IF;
                -- Match the child-write lock order used by Vault deletion.
                IF NEW.vault_id IS NOT NULL THEN
                    PERFORM id FROM vaults WHERE id = NEW.vault_id FOR KEY SHARE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'legacy adoption requires a live Vault'
                            USING ERRCODE = '23503';
                    END IF;
                END IF;
                IF NEW.installation_id IS NOT NULL THEN
                    SELECT vault_id INTO installation_vault
                      FROM vault_app_installations
                     WHERE id = NEW.installation_id FOR KEY SHARE;
                    IF NOT FOUND OR (NEW.vault_id IS NOT NULL
                                     AND installation_vault <> NEW.vault_id) THEN
                        RAISE EXCEPTION 'legacy adoption installation reference is invalid'
                            USING ERRCODE = '23503';
                    END IF;
                    -- An ownership-conflict audit may name another app's
                    -- installation. Preserve the former FK's semantics.
                END IF;
                RETURN NEW;
            END;
            $$;
            DROP TRIGGER IF EXISTS app_legacy_adoption_targets_live_references
                ON app_legacy_adoption_targets;
            CREATE TRIGGER app_legacy_adoption_targets_live_references
                BEFORE INSERT OR UPDATE ON app_legacy_adoption_targets
                FOR EACH ROW EXECUTE FUNCTION akb_validate_legacy_adoption_live_references();
            DROP TRIGGER IF EXISTS app_legacy_adoption_audit_live_references
                ON app_legacy_adoption_audit;
            CREATE TRIGGER app_legacy_adoption_audit_live_references
                BEFORE INSERT ON app_legacy_adoption_audit
                FOR EACH ROW EXECUTE FUNCTION akb_validate_legacy_adoption_live_references();
            REVOKE EXECUTE ON FUNCTION akb_validate_legacy_adoption_live_references() FROM PUBLIC;

            COMMENT ON COLUMN app_legacy_adoption_targets.vault_id IS
                'Historical Vault UUID; validated on insert, retained after Vault deletion.';
            COMMENT ON COLUMN app_legacy_adoption_targets.installation_id IS
                'Historical installation UUID; validated on assignment, retained after deletion.';
            COMMENT ON COLUMN app_legacy_adoption_audit.vault_id IS
                'Historical Vault UUID; validated on append, retained after Vault deletion.';
            COMMENT ON COLUMN app_legacy_adoption_audit.installation_id IS
                'Historical installation UUID; validated on append, retained after deletion.';
            """
        )
