"""Migration 062: durable app release rollout execution ledger.

The rollout tables are deliberately separate from the desired-state registry
and the sealed inventory snapshot.  A worker may be restarted at any point;
the rows below are the authority for claims, checkpoints, and replay.
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
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_rollout_jobs (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL REFERENCES app_definitions(id) ON DELETE RESTRICT,
                release_id UUID NOT NULL,
                manifest_checksum TEXT NOT NULL CHECK (manifest_checksum ~ '^[0-9a-f]{64}$'),
                idempotency_key UUID NOT NULL,
                snapshot_id UUID NOT NULL REFERENCES app_rollout_snapshots(id) ON DELETE RESTRICT,
                requested_by_kind TEXT NOT NULL DEFAULT 'admin',
                status TEXT NOT NULL DEFAULT 'pending',
                blocked_reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                UNIQUE (app_id, idempotency_key),
                UNIQUE (app_id, id),
                FOREIGN KEY (app_id, release_id)
                    REFERENCES app_releases(app_id, id) ON DELETE RESTRICT,
                CONSTRAINT app_rollout_jobs_requester_kind_check
                    CHECK (requested_by_kind IN ('admin','app')),
                CONSTRAINT app_rollout_jobs_status_check
                    CHECK (status IN ('pending','running','applied','blocked')),
                CONSTRAINT app_rollout_jobs_reason_check
                    CHECK (blocked_reason IS NULL OR blocked_reason ~ '^[a-z][a-z0-9_.:-]{0,63}$')
            );

            CREATE TABLE IF NOT EXISTS app_rollout_targets (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                job_id UUID NOT NULL REFERENCES app_rollout_jobs(id) ON DELETE CASCADE,
                app_id UUID NOT NULL,
                installation_id UUID NOT NULL,
                snapshot_target_id UUID NOT NULL
                    REFERENCES app_rollout_snapshot_targets(id) ON DELETE RESTRICT,
                vault_id UUID NOT NULL,
                release_id UUID NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                batch_no INTEGER NOT NULL CHECK (batch_no >= 0),
                is_canary BOOLEAN NOT NULL DEFAULT FALSE,
                state TEXT NOT NULL DEFAULT 'pending',
                reason_code TEXT,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                lease_owner TEXT,
                lease_expires_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (job_id, installation_id),
                UNIQUE (job_id, ordinal),
                FOREIGN KEY (installation_id)
                    REFERENCES vault_app_installations(id) ON DELETE CASCADE,
                FOREIGN KEY (app_id, release_id)
                    REFERENCES app_releases(app_id, id) ON DELETE RESTRICT,
                CONSTRAINT app_rollout_targets_state_check
                    CHECK (state IN ('pending','running','applied','replayed','failed','blocked')),
                CONSTRAINT app_rollout_targets_reason_check
                    CHECK (reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_.:-]{0,63}$')
            );

            CREATE INDEX IF NOT EXISTS app_rollout_targets_claim_idx
                ON app_rollout_targets(job_id, batch_no, ordinal, state);

            CREATE TABLE IF NOT EXISTS app_rollout_steps (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                job_id UUID NOT NULL REFERENCES app_rollout_jobs(id) ON DELETE CASCADE,
                target_id UUID NOT NULL REFERENCES app_rollout_targets(id) ON DELETE CASCADE,
                installation_id UUID NOT NULL,
                release_id UUID NOT NULL,
                step_id TEXT NOT NULL,
                step_order INTEGER NOT NULL CHECK (step_order >= 0),
                step_checksum TEXT NOT NULL CHECK (step_checksum ~ '^[0-9a-f]{64}$'),
                operation TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                lease_owner TEXT,
                lease_expires_at TIMESTAMPTZ,
                reason_code TEXT,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (installation_id, release_id, step_id),
                CONSTRAINT app_rollout_steps_state_check
                    CHECK (state IN ('pending','running','applied','replayed','failed','blocked')),
                CONSTRAINT app_rollout_steps_checkpoint_shape
                    CHECK (jsonb_typeof(checkpoint) = 'object'),
                CONSTRAINT app_rollout_steps_reason_check
                    CHECK (reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_.:-]{0,63}$')
            );

            CREATE INDEX IF NOT EXISTS app_rollout_steps_claim_idx
                ON app_rollout_steps(target_id, step_order, state);

            CREATE TABLE IF NOT EXISTS app_rollout_audit (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                job_id UUID REFERENCES app_rollout_jobs(id) ON DELETE CASCADE,
                app_id UUID NOT NULL REFERENCES app_definitions(id) ON DELETE RESTRICT,
                installation_id UUID,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason_code TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT app_rollout_audit_reason_check
                    CHECK (reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_.:-]{0,63}$')
            );
            CREATE INDEX IF NOT EXISTS app_rollout_audit_job_idx
                ON app_rollout_audit(job_id, created_at DESC);

            CREATE OR REPLACE FUNCTION akb_touch_app_rollout_row()
            RETURNS TRIGGER LANGUAGE plpgsql AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$;
            DROP TRIGGER IF EXISTS app_rollout_jobs_touch ON app_rollout_jobs;
            CREATE TRIGGER app_rollout_jobs_touch BEFORE UPDATE ON app_rollout_jobs
                FOR EACH ROW EXECUTE FUNCTION akb_touch_app_rollout_row();
            DROP TRIGGER IF EXISTS app_rollout_targets_touch ON app_rollout_targets;
            CREATE TRIGGER app_rollout_targets_touch BEFORE UPDATE ON app_rollout_targets
                FOR EACH ROW EXECUTE FUNCTION akb_touch_app_rollout_row();
            DROP TRIGGER IF EXISTS app_rollout_steps_touch ON app_rollout_steps;
            CREATE TRIGGER app_rollout_steps_touch BEFORE UPDATE ON app_rollout_steps
                FOR EACH ROW EXECUTE FUNCTION akb_touch_app_rollout_row();

            REVOKE ALL PRIVILEGES ON TABLE
                app_rollout_jobs,
                app_rollout_targets,
                app_rollout_steps,
                app_rollout_audit
            FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION akb_touch_app_rollout_row() FROM PUBLIC;
            """
        )
