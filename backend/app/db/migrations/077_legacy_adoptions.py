"""Migration 077: immutable legacy app adoption plans and target ledger.

Adoption is a control-plane metadata operation.  The ledger deliberately
stores the operator's explicit allowlist and bounded preflight results; it
does not copy table rows, alter physical tables, or issue grants/credentials.
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
    # Some upgrade fixtures represent a pre-registry database by marking the
    # historical migrations as applied while loading only init.sql.  Adoption
    # is meaningful only once the registry tables exist; keep that legacy
    # bootstrap path startable and let the migration ledger record this no-op.
    if not await conn.fetchval(
        """
        SELECT bool_and(to_regclass('public.' || table_name) IS NOT NULL)
          FROM unnest($1::text[]) AS required(table_name)
        """,
        ["app_definitions", "app_releases", "vault_app_installations", "app_owned_resources"],
    ):
        return

    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_legacy_adoption_plans (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL,
                baseline_release_id UUID NOT NULL,
                idempotency_key UUID NOT NULL,
                input_digest TEXT NOT NULL,
                input JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                requested_by TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                applied_at TIMESTAMPTZ,
                CONSTRAINT app_legacy_adoption_plans_app_fkey
                    FOREIGN KEY (app_id) REFERENCES app_definitions(id) ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_plans_release_fkey
                    FOREIGN KEY (app_id, baseline_release_id)
                    REFERENCES app_releases(app_id, id) ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_plans_key_unique
                    UNIQUE (app_id, idempotency_key),
                CONSTRAINT app_legacy_adoption_plans_app_id_id_key
                    UNIQUE (app_id, id),
                CONSTRAINT app_legacy_adoption_plans_digest_shape
                    CHECK (input_digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT app_legacy_adoption_plans_input_shape
                    CHECK (jsonb_typeof(input) = 'object'),
                CONSTRAINT app_legacy_adoption_plans_status_check
                    CHECK (status IN ('planned', 'partial', 'applied', 'blocked')),
                CONSTRAINT app_legacy_adoption_plans_requester_nonempty
                    CHECK (btrim(requested_by) <> '')
            );

            CREATE TABLE IF NOT EXISTS app_legacy_adoption_targets (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                adoption_id UUID NOT NULL,
                app_id UUID NOT NULL,
                vault_id UUID NOT NULL,
                target_order INTEGER NOT NULL,
                table_allowlist JSONB NOT NULL,
                expected_schema_fingerprint TEXT NOT NULL,
                actual_schema_fingerprint TEXT,
                included_tables JSONB NOT NULL DEFAULT '[]'::jsonb,
                excluded_tables JSONB NOT NULL DEFAULT '[]'::jsonb,
                missing_tables JSONB NOT NULL DEFAULT '[]'::jsonb,
                ownership_conflicts JSONB NOT NULL DEFAULT '[]'::jsonb,
                state TEXT NOT NULL DEFAULT 'planned',
                reason_code TEXT,
                installation_id UUID REFERENCES vault_app_installations(id) ON DELETE RESTRICT,
                checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
                planned_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT app_legacy_adoption_targets_adoption_fkey
                    FOREIGN KEY (adoption_id) REFERENCES app_legacy_adoption_plans(id)
                    ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_targets_app_adoption_fkey
                    FOREIGN KEY (app_id, adoption_id)
                    REFERENCES app_legacy_adoption_plans(app_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_targets_app_fkey
                    FOREIGN KEY (app_id) REFERENCES app_definitions(id) ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_targets_vault_fkey
                    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_targets_installation_vault_fkey
                    FOREIGN KEY (installation_id, vault_id)
                    REFERENCES vault_app_installations(id, vault_id)
                    ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_targets_order_unique
                    UNIQUE (adoption_id, target_order),
                CONSTRAINT app_legacy_adoption_targets_vault_unique
                    UNIQUE (adoption_id, vault_id),
                CONSTRAINT app_legacy_adoption_targets_app_id_id_key
                    UNIQUE (app_id, id),
                CONSTRAINT app_legacy_adoption_targets_order_check
                    CHECK (target_order >= 0),
                CONSTRAINT app_legacy_adoption_targets_allowlist_shape
                    CHECK (jsonb_typeof(table_allowlist) = 'array'
                           AND jsonb_array_length(table_allowlist) > 0),
                CONSTRAINT app_legacy_adoption_targets_expected_shape
                    CHECK (expected_schema_fingerprint ~ '^[0-9a-f]{64}$'),
                CONSTRAINT app_legacy_adoption_targets_actual_shape
                    CHECK (actual_schema_fingerprint IS NULL
                           OR actual_schema_fingerprint ~ '^[0-9a-f]{64}$'),
                CONSTRAINT app_legacy_adoption_targets_state_check
                    CHECK (state IN ('planned', 'applied', 'replayed', 'blocked')),
                CONSTRAINT app_legacy_adoption_targets_reason_check
                    CHECK (reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_.:-]{0,63}$'),
                CONSTRAINT app_legacy_adoption_targets_json_shape
                    CHECK (jsonb_typeof(included_tables) = 'array'
                           AND jsonb_typeof(excluded_tables) = 'array'
                           AND jsonb_typeof(missing_tables) = 'array'
                           AND jsonb_typeof(ownership_conflicts) = 'array'
                           AND jsonb_typeof(checkpoint) = 'object'
                           AND jsonb_typeof(planned_metadata) = 'object')
            );

            CREATE TABLE IF NOT EXISTS app_legacy_adoption_audit (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL,
                adoption_id UUID NOT NULL,
                target_id UUID,
                installation_id UUID REFERENCES vault_app_installations(id) ON DELETE RESTRICT,
                vault_id UUID,
                release_id UUID NOT NULL,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT app_legacy_adoption_audit_app_fkey
                    FOREIGN KEY (app_id) REFERENCES app_definitions(id) ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_audit_adoption_fkey
                    FOREIGN KEY (adoption_id) REFERENCES app_legacy_adoption_plans(id)
                    ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_audit_target_fkey
                    FOREIGN KEY (target_id) REFERENCES app_legacy_adoption_targets(id)
                    ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_audit_app_adoption_fkey
                    FOREIGN KEY (app_id, adoption_id)
                    REFERENCES app_legacy_adoption_plans(app_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_audit_vault_fkey
                    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_audit_target_app_fkey
                    FOREIGN KEY (app_id, target_id)
                    REFERENCES app_legacy_adoption_targets(app_id, id)
                    ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_audit_installation_vault_fkey
                    FOREIGN KEY (installation_id, vault_id)
                    REFERENCES vault_app_installations(id, vault_id)
                    ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_audit_release_fkey
                    FOREIGN KEY (app_id, release_id)
                    REFERENCES app_releases(app_id, id) ON DELETE RESTRICT,
                CONSTRAINT app_legacy_adoption_audit_action_check
                    CHECK (action IN (
                        'plan_created', 'plan_replayed', 'target_applied',
                        'target_replayed', 'target_blocked', 'resource_adopted',
                        'ownership_denied'
                    )),
                CONSTRAINT app_legacy_adoption_audit_outcome_check
                    CHECK (outcome IN ('ok', 'error', 'replay')),
                CONSTRAINT app_legacy_adoption_audit_reason_check
                    CHECK (reason_code ~ '^[a-z][a-z0-9_.:-]{0,63}$'),
                CONSTRAINT app_legacy_adoption_audit_correlation_check
                    CHECK (btrim(correlation_id) <> '')
            );

            CREATE INDEX IF NOT EXISTS app_legacy_adoption_targets_app_idx
                ON app_legacy_adoption_targets(app_id, vault_id, target_order);
            CREATE INDEX IF NOT EXISTS app_legacy_adoption_targets_installation_idx
                ON app_legacy_adoption_targets(installation_id)
                WHERE installation_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS app_legacy_adoption_audit_adoption_idx
                ON app_legacy_adoption_audit(adoption_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS app_legacy_adoption_audit_target_idx
                ON app_legacy_adoption_audit(target_id, created_at DESC)
                WHERE target_id IS NOT NULL;
            """
        )

        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_legacy_adoption_set_updated_at()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_legacy_adoption_plans_set_updated_at
                ON app_legacy_adoption_plans;
            CREATE TRIGGER app_legacy_adoption_plans_set_updated_at
                BEFORE UPDATE ON app_legacy_adoption_plans
                FOR EACH ROW EXECUTE FUNCTION akb_legacy_adoption_set_updated_at();

            DROP TRIGGER IF EXISTS app_legacy_adoption_targets_set_updated_at
                ON app_legacy_adoption_targets;
            CREATE TRIGGER app_legacy_adoption_targets_set_updated_at
                BEFORE UPDATE ON app_legacy_adoption_targets
                FOR EACH ROW EXECUTE FUNCTION akb_legacy_adoption_set_updated_at();

            CREATE OR REPLACE FUNCTION akb_guard_legacy_adoption_plan_identity()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.app_id IS DISTINCT FROM OLD.app_id
                   OR NEW.baseline_release_id IS DISTINCT FROM OLD.baseline_release_id
                   OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
                   OR NEW.input_digest IS DISTINCT FROM OLD.input_digest
                   OR NEW.input IS DISTINCT FROM OLD.input
                   OR NEW.requested_by IS DISTINCT FROM OLD.requested_by
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                THEN
                    RAISE EXCEPTION 'legacy adoption plan identity is immutable'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_legacy_adoption_plans_identity
                ON app_legacy_adoption_plans;
            CREATE TRIGGER app_legacy_adoption_plans_identity
                BEFORE UPDATE ON app_legacy_adoption_plans
                FOR EACH ROW EXECUTE FUNCTION akb_guard_legacy_adoption_plan_identity();

            CREATE OR REPLACE FUNCTION akb_guard_legacy_adoption_target_identity()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                plan_app_id UUID;
            BEGIN
                SELECT app_id INTO plan_app_id
                  FROM app_legacy_adoption_plans
                 WHERE id = NEW.adoption_id;
                IF plan_app_id IS NULL OR NEW.app_id IS DISTINCT FROM plan_app_id THEN
                    RAISE EXCEPTION 'legacy adoption target app identity is invalid'
                        USING ERRCODE = '23503';
                END IF;
                IF TG_OP = 'UPDATE' THEN
                    IF NEW.id IS DISTINCT FROM OLD.id
                       OR NEW.adoption_id IS DISTINCT FROM OLD.adoption_id
                       OR NEW.app_id IS DISTINCT FROM OLD.app_id
                       OR NEW.vault_id IS DISTINCT FROM OLD.vault_id
                       OR NEW.target_order IS DISTINCT FROM OLD.target_order
                       OR NEW.table_allowlist IS DISTINCT FROM OLD.table_allowlist
                       OR NEW.expected_schema_fingerprint IS DISTINCT FROM OLD.expected_schema_fingerprint
                       OR NEW.created_at IS DISTINCT FROM OLD.created_at
                    THEN
                        RAISE EXCEPTION 'legacy adoption target identity is immutable'
                            USING ERRCODE = '55000';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS app_legacy_adoption_targets_identity
                ON app_legacy_adoption_targets;
            CREATE TRIGGER app_legacy_adoption_targets_identity
                BEFORE INSERT OR UPDATE ON app_legacy_adoption_targets
                FOR EACH ROW EXECUTE FUNCTION akb_guard_legacy_adoption_target_identity();

            CREATE OR REPLACE FUNCTION akb_reject_legacy_adoption_delete()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'legacy adoption ledger rows are retained'
                    USING ERRCODE = '55000';
            END;
            $$;

            DROP TRIGGER IF EXISTS app_legacy_adoption_plans_no_delete
                ON app_legacy_adoption_plans;
            CREATE TRIGGER app_legacy_adoption_plans_no_delete
                BEFORE DELETE ON app_legacy_adoption_plans
                FOR EACH ROW EXECUTE FUNCTION akb_reject_legacy_adoption_delete();

            DROP TRIGGER IF EXISTS app_legacy_adoption_targets_no_delete
                ON app_legacy_adoption_targets;
            CREATE TRIGGER app_legacy_adoption_targets_no_delete
                BEFORE DELETE ON app_legacy_adoption_targets
                FOR EACH ROW EXECUTE FUNCTION akb_reject_legacy_adoption_delete();

            DROP TRIGGER IF EXISTS app_legacy_adoption_audit_no_mutation
                ON app_legacy_adoption_audit;
            CREATE TRIGGER app_legacy_adoption_audit_no_mutation
                BEFORE UPDATE OR DELETE ON app_legacy_adoption_audit
                FOR EACH ROW EXECUTE FUNCTION akb_reject_legacy_adoption_delete();

            REVOKE ALL PRIVILEGES ON TABLE
                app_legacy_adoption_plans,
                app_legacy_adoption_targets,
                app_legacy_adoption_audit
            FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_legacy_adoption_set_updated_at()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_legacy_adoption_plan_identity()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_guard_legacy_adoption_target_identity()
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_reject_legacy_adoption_delete()
                FROM PUBLIC;
            """
        )
