"""Migration 071: immutable-source rollout resume attempts."""

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
            ALTER TABLE app_rollout_jobs
                ADD COLUMN IF NOT EXISTS source_rollout_id UUID;

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_jobs_source_rollout_fkey'
                       AND conrelid = 'app_rollout_jobs'::regclass
                ) THEN
                    ALTER TABLE app_rollout_jobs
                        ADD CONSTRAINT app_rollout_jobs_source_rollout_fkey
                        FOREIGN KEY (source_rollout_id)
                        REFERENCES app_rollout_jobs(id)
                        ON DELETE RESTRICT;
                END IF;
            END
            $$;

            CREATE INDEX IF NOT EXISTS app_rollout_jobs_source_idx
                ON app_rollout_jobs(source_rollout_id);

            -- A resumed attempt deliberately reuses the immutable release
            -- steps.  The original ledger keyed a step only by installation,
            -- release, and step id, which made a new attempt collide with the
            -- blocked source row.  Scope uniqueness to the attempt instead;
            -- the job ledger remains the authority for replay and execution.
            ALTER TABLE app_rollout_steps
                DROP CONSTRAINT IF EXISTS app_rollout_steps_installation_id_release_id_step_id_key;
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'app_rollout_steps_job_installation_release_step_key'
                       AND conrelid = 'app_rollout_steps'::regclass
                ) THEN
                    ALTER TABLE app_rollout_steps
                        ADD CONSTRAINT app_rollout_steps_job_installation_release_step_key
                        UNIQUE (job_id, installation_id, release_id, step_id);
                END IF;
            END
            $$;

            CREATE TABLE IF NOT EXISTS app_rollout_resume_attempts (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL REFERENCES app_definitions(id) ON DELETE RESTRICT,
                source_rollout_id UUID NOT NULL,
                new_rollout_id UUID NOT NULL,
                idempotency_key UUID NOT NULL,
                release_id UUID NOT NULL,
                manifest_checksum TEXT NOT NULL CHECK (manifest_checksum ~ '^[0-9a-f]{64}$'),
                requested_by_kind TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'accepted',
                reason_code TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (source_rollout_id, idempotency_key),
                UNIQUE (app_id, new_rollout_id),
                FOREIGN KEY (app_id, source_rollout_id)
                    REFERENCES app_rollout_jobs(app_id, id) ON DELETE RESTRICT,
                FOREIGN KEY (app_id, new_rollout_id)
                    REFERENCES app_rollout_jobs(app_id, id) ON DELETE RESTRICT,
                FOREIGN KEY (app_id, release_id)
                    REFERENCES app_releases(app_id, id) ON DELETE RESTRICT,
                CONSTRAINT app_rollout_resume_requester_kind_check
                    CHECK (requested_by_kind IN ('admin','app')),
                CONSTRAINT app_rollout_resume_outcome_check
                    CHECK (outcome IN ('accepted','replayed','denied')),
                CONSTRAINT app_rollout_resume_reason_check
                    CHECK (reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_.:-]{0,63}$')
            );

            REVOKE ALL PRIVILEGES ON TABLE app_rollout_resume_attempts FROM PUBLIC;
            """
        )
