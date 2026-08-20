"""Enforce one-vault ownership for knowledge-graph edges.

The graph service treats a vault as one authorization and lifecycle boundary.
Both endpoint URI authorities must therefore match ``edges.vault_id``. Existing
cross-vault rows are retained for an operator-reviewed cleanup and hidden by
read-side filtering; the trigger prevents new rows and valid-to-invalid updates.
"""

from __future__ import annotations

import logging


logger = logging.getLogger("akb.migration.083")


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_enforce_edge_vault_boundary()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            SET search_path = pg_catalog, public
            AS $$
            DECLARE
                owner_name TEXT;
                source_name TEXT;
                target_name TEXT;
                old_source_name TEXT;
                old_target_name TEXT;
                new_is_valid BOOLEAN;
                old_is_valid BOOLEAN := FALSE;
            BEGIN
                SELECT name INTO owner_name FROM public.vaults WHERE id = NEW.vault_id;
                source_name := split_part(substr(NEW.source_uri, 7), '/', 1);
                target_name := split_part(substr(NEW.target_uri, 7), '/', 1);
                new_is_valid :=
                    starts_with(NEW.source_uri, 'akb://')
                    AND starts_with(NEW.target_uri, 'akb://')
                    AND source_name = owner_name
                    AND target_name = owner_name;

                IF new_is_valid THEN
                    RETURN NEW;
                END IF;

                -- A rollout may encounter a legacy invalid row while moving a
                -- document. Let an already-invalid row remain invalid so an
                -- unrelated lifecycle write is not blocked; read paths hide it
                -- and the operator audit below identifies it for cleanup.
                IF TG_OP = 'UPDATE' AND NEW.vault_id = OLD.vault_id THEN
                    old_source_name := split_part(substr(OLD.source_uri, 7), '/', 1);
                    old_target_name := split_part(substr(OLD.target_uri, 7), '/', 1);
                    old_is_valid :=
                        starts_with(OLD.source_uri, 'akb://')
                        AND starts_with(OLD.target_uri, 'akb://')
                        AND old_source_name = owner_name
                        AND old_target_name = owner_name;
                    IF NOT old_is_valid
                       AND source_name = old_source_name
                       AND target_name = old_target_name THEN
                        RETURN NEW;
                    END IF;
                END IF;

                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'edge endpoints must belong to the owning vault';
            END;
            $$
            """
        )
        await conn.execute("DROP TRIGGER IF EXISTS trg_edges_vault_boundary ON edges")
        await conn.execute(
            """
            CREATE TRIGGER trg_edges_vault_boundary
            BEFORE INSERT OR UPDATE OF vault_id, source_uri, target_uri
            ON edges
            FOR EACH ROW
            EXECUTE FUNCTION akb_enforce_edge_vault_boundary()
            """
        )

        legacy_count = await conn.fetchval(
            """
            SELECT count(*)
            FROM edges e
            JOIN vaults v ON v.id = e.vault_id
            WHERE NOT starts_with(e.source_uri, 'akb://' || v.name || '/')
               OR NOT starts_with(e.target_uri, 'akb://' || v.name || '/')
            """
        )

    if legacy_count:
        logger.warning(
            "Detected %d legacy edge row(s) outside their owning vault; "
            "reader projections exclude them until operator-reviewed cleanup",
            legacy_count,
        )
