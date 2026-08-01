"""Migration 048: PostgreSQL-native Resource/Revision ledger for M1 B-core.

The ``m1_reference_payloads`` relation is an explicitly experimental,
verified payload adapter used to exercise the semantic ledger.  Its presence
does not select the final searchable-body physical layout.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.048")


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
            CREATE TABLE IF NOT EXISTS m1_reference_payloads (
                payload_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                namespace_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                content_profile TEXT NOT NULL DEFAULT 'text',
                digest TEXT NOT NULL,
                byte_size BIGINT NOT NULL,
                encoding TEXT NOT NULL DEFAULT 'utf-8',
                selected_placement TEXT NOT NULL DEFAULT 'm1-reference-payload-v1',
                verification_profile TEXT NOT NULL DEFAULT 'sha256-size-utf8-v1',
                canonical_bytes BYTEA NOT NULL,
                prepared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT m1_reference_payloads_profile_check
                    CHECK (content_profile = 'text'),
                CONSTRAINT m1_reference_payloads_digest_shape
                    CHECK (digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT m1_reference_payloads_size_check
                    CHECK (byte_size >= 0 AND octet_length(canonical_bytes) = byte_size),
                CONSTRAINT m1_reference_payloads_digest_matches
                    CHECK (encode(digest(canonical_bytes, 'sha256'), 'hex') = digest),
                CONSTRAINT m1_reference_payloads_text_check
                    CHECK (
                        encoding = 'utf-8'
                        AND convert_from(canonical_bytes, 'UTF8') IS NOT NULL
                        AND position(decode('00', 'hex') IN canonical_bytes) = 0
                    ),
                CONSTRAINT m1_reference_payloads_placement_check
                    CHECK (selected_placement = 'm1-reference-payload-v1'),
                CONSTRAINT m1_reference_payloads_verification_check
                    CHECK (verification_profile = 'sha256-size-utf8-v1'),
                CONSTRAINT m1_reference_payloads_dedup_key
                    UNIQUE (namespace_id, digest, byte_size),
                CONSTRAINT m1_reference_payloads_manifest_identity
                    UNIQUE (
                        payload_id, namespace_id, content_profile, digest,
                        byte_size, encoding, selected_placement,
                        verification_profile
                    )
            );

            CREATE TABLE IF NOT EXISTS native_resources (
                resource_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                namespace_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                surface TEXT NOT NULL,
                content_profile TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'native',
                mutability TEXT NOT NULL DEFAULT 'akb_writable',
                current_path TEXT NOT NULL,
                lifecycle TEXT NOT NULL DEFAULT 'live',
                head_revision_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT native_resources_surface_check
                    CHECK (surface IN ('document', 'file')),
                CONSTRAINT native_resources_content_profile_check
                    CHECK (content_profile IN ('text', 'binary')),
                CONSTRAINT native_resources_origin_check
                    CHECK (origin = 'native'),
                CONSTRAINT native_resources_mutability_check
                    CHECK (mutability = 'akb_writable'),
                CONSTRAINT native_resources_path_nonempty
                    CHECK (btrim(current_path) <> ''),
                CONSTRAINT native_resources_lifecycle_check
                    CHECK (lifecycle IN ('live', 'deleted')),
                CONSTRAINT native_resources_namespace_resource_key
                    UNIQUE (namespace_id, resource_id),
                CONSTRAINT native_resources_namespace_surface_resource_key
                    UNIQUE (namespace_id, surface, resource_id)
            );

            DROP INDEX IF EXISTS uq_native_resources_live_path;
            CREATE UNIQUE INDEX uq_native_resources_live_path
                ON native_resources(namespace_id, current_path)
                WHERE lifecycle = 'live';

            CREATE TABLE IF NOT EXISTS native_payload_manifests (
                payload_manifest_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                namespace_id UUID NOT NULL,
                resource_id UUID NOT NULL,
                content_profile TEXT NOT NULL,
                digest TEXT NOT NULL,
                byte_size BIGINT NOT NULL,
                encoding TEXT NOT NULL,
                selected_placement TEXT NOT NULL,
                private_locator UUID NOT NULL,
                verification_profile TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT native_payload_manifests_digest_shape
                    CHECK (digest ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_payload_manifests_size_check
                    CHECK (byte_size >= 0),
                CONSTRAINT native_payload_manifests_resource_fkey
                    FOREIGN KEY (namespace_id, resource_id)
                    REFERENCES native_resources(namespace_id, resource_id)
                    ON DELETE CASCADE,
                CONSTRAINT native_payload_manifests_reference_fkey
                    FOREIGN KEY (
                        private_locator, namespace_id, content_profile, digest,
                        byte_size, encoding, selected_placement,
                        verification_profile
                    )
                    REFERENCES m1_reference_payloads(
                        payload_id, namespace_id, content_profile, digest,
                        byte_size, encoding, selected_placement,
                        verification_profile
                    )
                    ON DELETE NO ACTION
                    DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT native_payload_manifests_resource_manifest_key
                    UNIQUE (resource_id, payload_manifest_id)
            );

            CREATE TABLE IF NOT EXISTS native_revisions (
                revision_id TEXT PRIMARY KEY,
                namespace_id UUID NOT NULL,
                resource_id UUID NOT NULL,
                parent_revision_id TEXT,
                action TEXT NOT NULL,
                path_at_revision TEXT NOT NULL,
                path_from TEXT,
                path_to TEXT,
                payload_manifest_id UUID,
                mutation_id UUID NOT NULL,
                request_fingerprint TEXT NOT NULL,
                message TEXT,
                subject TEXT,
                summary TEXT,
                actor TEXT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                activity_event_id UUID NOT NULL,
                invalidation_intent_id UUID NOT NULL,
                CONSTRAINT native_revisions_revision_id_shape
                    CHECK (revision_id ~ '^[0-9a-f]{40}$'),
                CONSTRAINT native_revisions_action_check
                    CHECK (action IN ('create', 'replace', 'move', 'delete')),
                CONSTRAINT native_revisions_path_nonempty
                    CHECK (btrim(path_at_revision) <> ''),
                CONSTRAINT native_revisions_fingerprint_shape
                    CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
                CONSTRAINT native_revisions_resource_fkey
                    FOREIGN KEY (namespace_id, resource_id)
                    REFERENCES native_resources(namespace_id, resource_id)
                    ON DELETE CASCADE,
                CONSTRAINT native_revisions_parent_fkey
                    FOREIGN KEY (resource_id, parent_revision_id)
                    REFERENCES native_revisions(resource_id, revision_id)
                    ON DELETE NO ACTION
                    DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT native_revisions_manifest_fkey
                    FOREIGN KEY (resource_id, payload_manifest_id)
                    REFERENCES native_payload_manifests(resource_id, payload_manifest_id)
                    ON DELETE NO ACTION
                    DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT native_revisions_action_shape
                    CHECK (
                        (
                            action = 'create'
                            AND parent_revision_id IS NULL
                            AND payload_manifest_id IS NOT NULL
                            AND path_from IS NULL
                            AND path_to = path_at_revision
                        )
                        OR (
                            action = 'replace'
                            AND parent_revision_id IS NOT NULL
                            AND payload_manifest_id IS NOT NULL
                            AND path_from IS NULL
                            AND path_to IS NULL
                        )
                        OR (
                            action = 'move'
                            AND parent_revision_id IS NOT NULL
                            AND payload_manifest_id IS NOT NULL
                            AND path_from IS NOT NULL
                            AND path_to = path_at_revision
                            AND path_from <> path_to
                        )
                        OR (
                            action = 'delete'
                            AND parent_revision_id IS NOT NULL
                            AND payload_manifest_id IS NULL
                            AND path_from IS NULL
                            AND path_to IS NULL
                        )
                    ),
                CONSTRAINT native_revisions_namespace_mutation_key
                    UNIQUE (namespace_id, mutation_id),
                CONSTRAINT native_revisions_namespace_resource_revision_key
                    UNIQUE (namespace_id, resource_id, revision_id),
                CONSTRAINT native_revisions_resource_revision_key
                    UNIQUE (resource_id, revision_id),
                CONSTRAINT native_revisions_activity_key
                    UNIQUE (activity_event_id),
                CONSTRAINT native_revisions_intent_key
                    UNIQUE (invalidation_intent_id),
                CONSTRAINT native_revisions_manifest_key
                    UNIQUE (payload_manifest_id)
            );

            CREATE INDEX IF NOT EXISTS idx_native_revisions_resource_history
                ON native_revisions(resource_id, occurred_at DESC, revision_id DESC);

            CREATE TABLE IF NOT EXISTS native_revision_activity (
                activity_event_id UUID PRIMARY KEY,
                namespace_id UUID NOT NULL,
                resource_id UUID NOT NULL,
                revision_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                subject TEXT,
                summary TEXT,
                changed_path_from TEXT,
                changed_path_to TEXT,
                occurred_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT native_revision_activity_action_check
                    CHECK (action IN ('create', 'replace', 'move', 'delete')),
                CONSTRAINT native_revision_activity_revision_fkey
                    FOREIGN KEY (namespace_id, resource_id, revision_id)
                    REFERENCES native_revisions(namespace_id, resource_id, revision_id)
                    ON DELETE CASCADE
                    DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT native_revision_activity_revision_key
                    UNIQUE (revision_id),
                CONSTRAINT native_revision_activity_link_key
                    UNIQUE (
                        activity_event_id, namespace_id, resource_id,
                        revision_id, action
                    )
            );

            CREATE INDEX IF NOT EXISTS idx_native_revision_activity_resource
                ON native_revision_activity(resource_id, occurred_at DESC, revision_id DESC);

            CREATE TABLE IF NOT EXISTS native_invalidation_intents (
                intent_id UUID PRIMARY KEY,
                namespace_id UUID NOT NULL,
                resource_id UUID NOT NULL,
                revision_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                selected_delivery TEXT,
                occurred_at TIMESTAMPTZ NOT NULL,
                claimed_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                last_error TEXT,
                CONSTRAINT native_invalidation_intents_reason_check
                    CHECK (reason IN ('create', 'replace', 'move', 'delete')),
                CONSTRAINT native_invalidation_intents_revision_fkey
                    FOREIGN KEY (namespace_id, resource_id, revision_id)
                    REFERENCES native_revisions(namespace_id, resource_id, revision_id)
                    ON DELETE CASCADE
                    DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT native_invalidation_intents_revision_key
                    UNIQUE (revision_id),
                CONSTRAINT native_invalidation_intents_link_key
                    UNIQUE (
                        intent_id, namespace_id, resource_id,
                        revision_id, reason
                    )
            );

            CREATE INDEX IF NOT EXISTS idx_native_invalidation_pending
                ON native_invalidation_intents(occurred_at, intent_id)
                WHERE completed_at IS NULL;

            CREATE TABLE IF NOT EXISTS native_resource_path_aliases (
                alias_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                namespace_id UUID NOT NULL,
                surface TEXT NOT NULL,
                old_path TEXT NOT NULL,
                resource_id UUID NOT NULL,
                created_revision_id TEXT NOT NULL,
                retired_revision_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                retired_at TIMESTAMPTZ,
                CONSTRAINT native_resource_path_aliases_surface_check
                    CHECK (surface IN ('document', 'file')),
                CONSTRAINT native_resource_path_aliases_path_nonempty
                    CHECK (btrim(old_path) <> ''),
                CONSTRAINT native_resource_path_aliases_retirement_check
                    CHECK (
                        (retired_revision_id IS NULL AND retired_at IS NULL)
                        OR (retired_revision_id IS NOT NULL AND retired_at IS NOT NULL)
                    ),
                CONSTRAINT native_resource_path_aliases_resource_fkey
                    FOREIGN KEY (namespace_id, surface, resource_id)
                    REFERENCES native_resources(namespace_id, surface, resource_id)
                    ON DELETE CASCADE,
                CONSTRAINT native_resource_path_aliases_created_revision_fkey
                    FOREIGN KEY (namespace_id, resource_id, created_revision_id)
                    REFERENCES native_revisions(namespace_id, resource_id, revision_id)
                    ON DELETE NO ACTION
                    DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT native_resource_path_aliases_retired_revision_fkey
                    FOREIGN KEY (namespace_id, resource_id, retired_revision_id)
                    REFERENCES native_revisions(namespace_id, resource_id, revision_id)
                    ON DELETE NO ACTION
                    DEFERRABLE INITIALLY DEFERRED
            );

            DROP INDEX IF EXISTS uq_native_resource_path_aliases_live;
            CREATE UNIQUE INDEX uq_native_resource_path_aliases_live
                ON native_resource_path_aliases(namespace_id, old_path)
                WHERE retired_revision_id IS NULL;

            CREATE INDEX IF NOT EXISTS idx_native_resource_path_aliases_resource
                ON native_resource_path_aliases(resource_id, created_at DESC);

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'native_resources_head_fkey'
                       AND conrelid = 'native_resources'::regclass
                ) THEN
                    ALTER TABLE native_resources
                        ADD CONSTRAINT native_resources_head_fkey
                        FOREIGN KEY (resource_id, head_revision_id)
                        REFERENCES native_revisions(resource_id, revision_id)
                        ON DELETE NO ACTION
                        DEFERRABLE INITIALLY DEFERRED;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'native_revisions_activity_fkey'
                       AND conrelid = 'native_revisions'::regclass
                ) THEN
                    ALTER TABLE native_revisions
                        ADD CONSTRAINT native_revisions_activity_fkey
                        FOREIGN KEY (
                            activity_event_id, namespace_id, resource_id,
                            revision_id, action
                        )
                        REFERENCES native_revision_activity(
                            activity_event_id, namespace_id, resource_id,
                            revision_id, action
                        )
                        ON DELETE NO ACTION
                        DEFERRABLE INITIALLY DEFERRED;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'native_revisions_intent_fkey'
                       AND conrelid = 'native_revisions'::regclass
                ) THEN
                    ALTER TABLE native_revisions
                        ADD CONSTRAINT native_revisions_intent_fkey
                        FOREIGN KEY (
                            invalidation_intent_id, namespace_id, resource_id,
                            revision_id, action
                        )
                        REFERENCES native_invalidation_intents(
                            intent_id, namespace_id, resource_id,
                            revision_id, reason
                        )
                        ON DELETE NO ACTION
                        DEFERRABLE INITIALLY DEFERRED;
                END IF;
            END
            $$;

            CREATE OR REPLACE FUNCTION akb_native_reject_immutable_fact_mutation()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' AND NOT EXISTS (
                    SELECT 1 FROM vaults WHERE id = OLD.namespace_id
                ) THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END;
            $$;

            CREATE OR REPLACE FUNCTION akb_native_validate_resource_head()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                observed_head TEXT;
            BEGIN
                SELECT head_revision_id
                  INTO observed_head
                  FROM native_resources
                 WHERE resource_id = NEW.resource_id;
                IF observed_head IS NULL THEN
                    RAISE EXCEPTION 'native Resource must publish one Head'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NULL;
            END;
            $$;

            DO $$
            DECLARE
                table_name TEXT;
                trigger_name TEXT;
            BEGIN
                FOREACH table_name IN ARRAY ARRAY[
                    'native_revisions',
                    'native_payload_manifests',
                    'native_revision_activity'
                ]
                LOOP
                    trigger_name := 'trg_' || table_name || '_immutable';
                    IF NOT EXISTS (
                        SELECT 1
                          FROM pg_trigger
                         WHERE tgname = trigger_name
                           AND tgrelid = table_name::regclass
                           AND NOT tgisinternal
                    ) THEN
                        EXECUTE format(
                            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
                            'FOR EACH ROW EXECUTE FUNCTION akb_native_reject_immutable_fact_mutation()',
                            trigger_name,
                            table_name
                        );
                    END IF;
                END LOOP;
            END
            $$;

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                      FROM pg_trigger
                     WHERE tgname = 'trg_native_resources_validate_head'
                       AND tgrelid = 'native_resources'::regclass
                       AND NOT tgisinternal
                ) THEN
                    CREATE CONSTRAINT TRIGGER trg_native_resources_validate_head
                    AFTER INSERT OR UPDATE ON native_resources
                    DEFERRABLE INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION akb_native_validate_resource_head();
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                      FROM pg_trigger
                     WHERE tgname = 'trg_m1_reference_payloads_immutable'
                       AND tgrelid = 'm1_reference_payloads'::regclass
                       AND NOT tgisinternal
                ) THEN
                    CREATE TRIGGER trg_m1_reference_payloads_immutable
                    BEFORE UPDATE ON m1_reference_payloads
                    FOR EACH ROW EXECUTE FUNCTION akb_native_reject_immutable_fact_mutation();
                END IF;
            END
            $$;
            """
        )
    logger.info("Migration 048: native revision M1 core ready")
