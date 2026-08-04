"""Migration 052: app inventory, observed state, and rollout snapshots.

The tables in this migration are control-plane state.  They deliberately keep
worker reports and rollout membership separate from the desired-state registry:
the registry remains the desired source of truth, while reports are monotonic
observations and snapshots are sealed membership ledgers.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("akb.migration.052")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_installation_observed_states (
                installation_id UUID PRIMARY KEY,
                app_id UUID NOT NULL,
                vault_id UUID NOT NULL,
                observed_generation BIGINT NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                observed_release_id UUID,
                observed_release_version TEXT,
                schema_fingerprint TEXT,
                observed_grant_generation BIGINT,
                checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
                recent_error JSONB,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_installation_fkey'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_installation_fkey
                        FOREIGN KEY (installation_id)
                        REFERENCES vault_app_installations(id)
                        ON DELETE CASCADE;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_app_fkey'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_app_fkey
                        FOREIGN KEY (app_id)
                        REFERENCES app_definitions(id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_vault_fkey'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_vault_fkey
                        FOREIGN KEY (vault_id)
                        REFERENCES vaults(id)
                        ON DELETE CASCADE;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_release_fkey'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_release_fkey
                        FOREIGN KEY (app_id, observed_release_id)
                        REFERENCES app_releases(app_id, id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_generation_check'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_generation_check
                        CHECK (observed_generation >= 0);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_grant_generation_check'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_grant_generation_check
                        CHECK (
                            observed_grant_generation IS NULL
                            OR observed_grant_generation >= 0
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_checkpoint_shape'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_checkpoint_shape
                        CHECK (jsonb_typeof(checkpoint) = 'object');
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_error_shape'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_error_shape
                        CHECK (
                            recent_error IS NULL
                            OR jsonb_typeof(recent_error) = 'object'
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_release_version_length'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_release_version_length
                        CHECK (observed_release_version IS NULL OR char_length(observed_release_version) <= 256);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_observed_states_schema_length'
                       AND conrelid = 'app_installation_observed_states'::regclass
                ) THEN
                    ALTER TABLE app_installation_observed_states
                        ADD CONSTRAINT app_observed_states_schema_length
                        CHECK (schema_fingerprint IS NULL OR char_length(schema_fingerprint) <= 256);
                END IF;
            END
            $$;

            CREATE INDEX IF NOT EXISTS app_observed_states_app_idx
                ON app_installation_observed_states(app_id, observed_at DESC);

            CREATE OR REPLACE FUNCTION akb_guard_observed_state_mutation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                installation_app_id UUID;
                installation_vault_id UUID;
            BEGIN
                SELECT app_id, vault_id
                  INTO installation_app_id, installation_vault_id
                  FROM vault_app_installations
                 WHERE id = NEW.installation_id;

                IF installation_app_id IS NULL THEN
                    RAISE EXCEPTION 'installation does not exist'
                        USING ERRCODE = '23503';
                END IF;
                IF NEW.app_id IS DISTINCT FROM installation_app_id
                   OR NEW.vault_id IS DISTINCT FROM installation_vault_id
                THEN
                    RAISE EXCEPTION
                        'observed state identity must match its installation'
                        USING ERRCODE = '23503';
                END IF;

                IF TG_OP = 'UPDATE' THEN
                    IF NEW.installation_id IS DISTINCT FROM OLD.installation_id
                       OR NEW.app_id IS DISTINCT FROM OLD.app_id
                       OR NEW.vault_id IS DISTINCT FROM OLD.vault_id
                    THEN
                        RAISE EXCEPTION
                            'observed state identity is immutable'
                            USING ERRCODE = '55000';
                    END IF;
                    IF NEW.observed_generation < OLD.observed_generation
                       OR NEW.observed_at < OLD.observed_at
                    THEN
                        RAISE EXCEPTION
                            'older observed state cannot replace a newer report'
                            USING ERRCODE = '55000';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_observed_states_mutation_guard
                ON app_installation_observed_states;
            CREATE TRIGGER app_observed_states_mutation_guard
                BEFORE INSERT OR UPDATE ON app_installation_observed_states
                FOR EACH ROW EXECUTE FUNCTION akb_guard_observed_state_mutation();
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_rollout_snapshots (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sealed_at TIMESTAMPTZ,
                requested_by_kind TEXT NOT NULL DEFAULT 'admin'
            );

            CREATE TABLE IF NOT EXISTS app_rollout_snapshot_targets (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                snapshot_id UUID NOT NULL,
                app_id UUID NOT NULL,
                installation_id UUID NOT NULL,
                vault_id UUID NOT NULL,
                desired_release_id UUID NOT NULL,
                current_release_id UUID,
                baseline_grant_generation BIGINT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                reason_code TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT app_rollout_snapshot_targets_snapshot_key
                    UNIQUE (snapshot_id, installation_id)
            );

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshots_app_fkey'
                       AND conrelid = 'app_rollout_snapshots'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshots
                        ADD CONSTRAINT app_rollout_snapshots_app_fkey
                        FOREIGN KEY (app_id)
                        REFERENCES app_definitions(id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshot_targets_snapshot_fkey'
                       AND conrelid = 'app_rollout_snapshot_targets'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshot_targets
                        ADD CONSTRAINT app_rollout_snapshot_targets_snapshot_fkey
                        FOREIGN KEY (snapshot_id)
                        REFERENCES app_rollout_snapshots(id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshot_targets_app_fkey'
                       AND conrelid = 'app_rollout_snapshot_targets'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshot_targets
                        ADD CONSTRAINT app_rollout_snapshot_targets_app_fkey
                        FOREIGN KEY (app_id)
                        REFERENCES app_definitions(id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshot_targets_desired_release_fkey'
                       AND conrelid = 'app_rollout_snapshot_targets'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshot_targets
                        ADD CONSTRAINT app_rollout_snapshot_targets_desired_release_fkey
                        FOREIGN KEY (app_id, desired_release_id)
                        REFERENCES app_releases(app_id, id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshot_targets_current_release_fkey'
                       AND conrelid = 'app_rollout_snapshot_targets'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshot_targets
                        ADD CONSTRAINT app_rollout_snapshot_targets_current_release_fkey
                        FOREIGN KEY (app_id, current_release_id)
                        REFERENCES app_releases(app_id, id)
                        ON DELETE RESTRICT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshots_requester_kind_check'
                       AND conrelid = 'app_rollout_snapshots'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshots
                        ADD CONSTRAINT app_rollout_snapshots_requester_kind_check
                        CHECK (requested_by_kind IN ('admin', 'app'));
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshot_targets_generation_check'
                       AND conrelid = 'app_rollout_snapshot_targets'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshot_targets
                        ADD CONSTRAINT app_rollout_snapshot_targets_generation_check
                        CHECK (baseline_grant_generation >= 0);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshot_targets_state_check'
                       AND conrelid = 'app_rollout_snapshot_targets'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshot_targets
                        ADD CONSTRAINT app_rollout_snapshot_targets_state_check
                        CHECK (
                            state IN (
                                'pending', 'running', 'applied', 'replayed',
                                'failed', 'skipped', 'denied'
                            )
                        );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_snapshot_targets_reason_check'
                       AND conrelid = 'app_rollout_snapshot_targets'::regclass
                ) THEN
                    ALTER TABLE app_rollout_snapshot_targets
                        ADD CONSTRAINT app_rollout_snapshot_targets_reason_check
                        CHECK (
                            reason_code IS NULL
                            OR reason_code ~ '^[a-z][a-z0-9_.-]{0,63}$'
                        );
                END IF;
            END
            $$;

            CREATE INDEX IF NOT EXISTS app_rollout_snapshots_app_idx
                ON app_rollout_snapshots(app_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS app_rollout_snapshot_targets_snapshot_idx
                ON app_rollout_snapshot_targets(snapshot_id, created_at, id);
            CREATE INDEX IF NOT EXISTS app_rollout_snapshot_targets_app_idx
                ON app_rollout_snapshot_targets(app_id, installation_id);

            CREATE OR REPLACE FUNCTION akb_app_inventory_set_updated_at()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_rollout_snapshot_targets_set_updated_at
                ON app_rollout_snapshot_targets;
            CREATE TRIGGER app_rollout_snapshot_targets_set_updated_at
                BEFORE UPDATE ON app_rollout_snapshot_targets
                FOR EACH ROW EXECUTE FUNCTION akb_app_inventory_set_updated_at();

            CREATE OR REPLACE FUNCTION akb_guard_rollout_snapshot_mutation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION
                        'sealed rollout snapshots are retained'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.app_id IS DISTINCT FROM OLD.app_id
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                   OR NEW.requested_by_kind IS DISTINCT FROM OLD.requested_by_kind
                THEN
                    RAISE EXCEPTION
                        'rollout snapshot identity is immutable'
                        USING ERRCODE = '55000';
                END IF;
                IF OLD.sealed_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'sealed rollout snapshots are immutable'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.sealed_at IS NULL THEN
                    RETURN NEW;
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_rollout_snapshots_immutable
                ON app_rollout_snapshots;
            CREATE TRIGGER app_rollout_snapshots_immutable
                BEFORE UPDATE OR DELETE ON app_rollout_snapshots
                FOR EACH ROW EXECUTE FUNCTION akb_guard_rollout_snapshot_mutation();

            CREATE OR REPLACE FUNCTION akb_guard_rollout_snapshot_target_mutation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                snapshot_app_id UUID;
                snapshot_sealed_at TIMESTAMPTZ;
                installation_app_id UUID;
                installation_vault_id UUID;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION
                        'rollout snapshot membership is immutable'
                        USING ERRCODE = '55000';
                END IF;

                SELECT app_id, sealed_at
                  INTO snapshot_app_id, snapshot_sealed_at
                  FROM app_rollout_snapshots
                 WHERE id = NEW.snapshot_id;
                IF snapshot_app_id IS NULL THEN
                    RAISE EXCEPTION 'rollout snapshot does not exist'
                        USING ERRCODE = '23503';
                END IF;
                IF NEW.app_id IS DISTINCT FROM snapshot_app_id THEN
                    RAISE EXCEPTION
                        'rollout target app must match its snapshot'
                        USING ERRCODE = '23503';
                END IF;

                IF TG_OP = 'INSERT' THEN
                    IF snapshot_sealed_at IS NOT NULL THEN
                        RAISE EXCEPTION
                            'sealed rollout snapshot membership is immutable'
                            USING ERRCODE = '55000';
                    END IF;
                    SELECT app_id, vault_id
                      INTO installation_app_id, installation_vault_id
                      FROM vault_app_installations
                     WHERE id = NEW.installation_id;
                    IF installation_app_id IS NULL THEN
                        RAISE EXCEPTION 'installation does not exist'
                            USING ERRCODE = '23503';
                    END IF;
                    IF NEW.app_id IS DISTINCT FROM installation_app_id
                       OR NEW.vault_id IS DISTINCT FROM installation_vault_id
                    THEN
                        RAISE EXCEPTION
                            'rollout target identity must match its installation'
                            USING ERRCODE = '23503';
                    END IF;
                    RETURN NEW;
                END IF;

                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id
                   OR NEW.app_id IS DISTINCT FROM OLD.app_id
                   OR NEW.installation_id IS DISTINCT FROM OLD.installation_id
                   OR NEW.vault_id IS DISTINCT FROM OLD.vault_id
                   OR NEW.desired_release_id IS DISTINCT FROM OLD.desired_release_id
                   OR NEW.current_release_id IS DISTINCT FROM OLD.current_release_id
                   OR NEW.baseline_grant_generation IS DISTINCT FROM OLD.baseline_grant_generation
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                THEN
                    RAISE EXCEPTION
                        'rollout target identity and baseline are immutable'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_rollout_snapshot_targets_immutable
                ON app_rollout_snapshot_targets;
            CREATE TRIGGER app_rollout_snapshot_targets_immutable
                BEFORE INSERT OR UPDATE OR DELETE ON app_rollout_snapshot_targets
                FOR EACH ROW EXECUTE FUNCTION akb_guard_rollout_snapshot_target_mutation();

            REVOKE ALL PRIVILEGES ON TABLE
                app_installation_observed_states,
                app_rollout_snapshots,
                app_rollout_snapshot_targets
            FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_observed_state_mutation()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_app_inventory_set_updated_at()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_rollout_snapshot_mutation()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_rollout_snapshot_target_mutation()
                FROM PUBLIC;
            """
        )

    logger.info("Migration 052 added app inventory and rollout snapshot state")


async def _main():
    from app.db.postgres import close_pool, init_db

    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
