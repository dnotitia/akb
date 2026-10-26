-- Fresh human authorization creates a new credential/nonce namespace. It never
-- extends the old grant or rewrites the original source/target intent.
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS parent_operation_id UUID REFERENCES sso_identity_operations(operation_id);
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS intent_sha256 TEXT;
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS authority_attempt BIGINT NOT NULL DEFAULT 1 CHECK(authority_attempt > 0);
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS lineage_closed_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS sso_identity_operation_attempt_unique
    ON sso_identity_operations(parent_operation_id,authority_attempt) WHERE parent_operation_id IS NOT NULL;
-- Preserve deliberate retirement made before attempts existed. Superseded
-- roots already have a child and remain recoverable until explicitly closed.
UPDATE sso_identity_operations p SET lineage_closed_at=p.retired_at
 WHERE p.parent_operation_id IS NULL AND p.retired_at IS NOT NULL AND p.lineage_closed_at IS NULL
   AND NOT EXISTS(SELECT 1 FROM sso_identity_operations c WHERE c.parent_operation_id=p.operation_id);
