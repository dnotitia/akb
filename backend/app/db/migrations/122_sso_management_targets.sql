-- Keep the original active realm's ownership UUID stable across epoch changes.
ALTER TABLE sso_operation_installation ADD COLUMN IF NOT EXISTS legacy_target_id UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE sso_operation_installation ADD COLUMN IF NOT EXISTS legacy_issuer TEXT;
ALTER TABLE sso_provider_ownership ADD COLUMN IF NOT EXISTS target_id UUID;
UPDATE sso_provider_ownership SET target_id=(SELECT legacy_target_id FROM sso_operation_installation) WHERE target_id IS NULL;
ALTER TABLE sso_provider_ownership ALTER COLUMN target_id SET NOT NULL;
ALTER TABLE sso_provider_ownership DROP CONSTRAINT IF EXISTS sso_provider_ownership_pkey;
ALTER TABLE sso_provider_ownership ADD PRIMARY KEY(target_id,alias);
ALTER TABLE sso_identity_operations ALTER COLUMN session_epoch DROP NOT NULL;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS target_id UUID;
UPDATE sso_identity_operations SET target_id=(SELECT legacy_target_id FROM sso_operation_installation) WHERE target_id IS NULL;
ALTER TABLE sso_identity_operations ALTER COLUMN target_id SET NOT NULL;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS target_fingerprint TEXT;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS target_realm_id TEXT;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS source_tuple JSONB;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS target_tuple JSONB;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'granted';
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS prepared_identity_ids_hash TEXT;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS existing_identity_ids_hash TEXT;
ALTER TABLE sso_provider_ownership ADD COLUMN IF NOT EXISTS secret_revision TEXT;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS provider_secret_revision TEXT;
-- Identity namespaces survive config removal; an issuer cannot be silently
-- re-registered under a new UUID to hide its existing ownership rows.
CREATE TABLE IF NOT EXISTS sso_management_target_pins (
    target_id UUID PRIMARY KEY,
    issuer TEXT NOT NULL UNIQUE
);
-- A pre-target grant has no immutable target/source pin. Never reinterpret it
-- against new settings; retain its journal as retired metadata for a human.
UPDATE sso_identity_operations SET retired_at=COALESCE(retired_at,clock_timestamp()),phase='retired' WHERE source_tuple IS NULL;
