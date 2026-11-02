-- A service key proposes; a human administrator approves the exact digest.
-- Not to be confused with sso_identity_operation_requests, which holds the
-- per-nonce execution receipts of an already issued operation. A request row
-- never holds a credential: an approved grant's operation row, created when the
-- requesting token collects it, keeps only the credential hash, and this row
-- keeps only the recipient's public key.
CREATE TABLE IF NOT EXISTS sso_operation_requests (
    request_id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('ownership_claim', 'provider_grant')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'cancelled', 'expired', 'collected')),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    target_id UUID NOT NULL,
    provider_alias TEXT NOT NULL,
    requested_by UUID NOT NULL,
    requested_token_id UUID NOT NULL,
    operation_id UUID UNIQUE,
    ttl_seconds INTEGER CHECK (ttl_seconds BETWEEN 1 AND 600),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_by UUID,
    decided_at TIMESTAMPTZ,
    operation_expires_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    CHECK (expires_at > created_at),
    CHECK ((kind = 'provider_grant') = (operation_id IS NOT NULL AND ttl_seconds IS NOT NULL)),
    CHECK (kind = 'provider_grant' OR status <> 'collected'),
    CHECK (status NOT IN ('approved', 'denied', 'collected') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
    -- A grant's operation never outlives its human approval by more than its TTL.
    CHECK (operation_expires_at IS NULL
           OR (kind = 'provider_grant' AND operation_expires_at = decided_at + ttl_seconds * INTERVAL '1 second')),
    CHECK (status = 'pending' OR closed_at IS NOT NULL OR status = 'approved')
);
CREATE INDEX IF NOT EXISTS sso_operation_requests_open
    ON sso_operation_requests (status, created_at) WHERE status IN ('pending', 'approved');
CREATE INDEX IF NOT EXISTS sso_operation_requests_token
    ON sso_operation_requests (requested_token_id, created_at);
-- The secret-free configure summary a human approved. Execution of a
-- configure verb requires every non-secret payload field to equal it. Direct
-- grants leave it null and keep their existing behavior.
ALTER TABLE sso_identity_operations ADD COLUMN IF NOT EXISTS provider_configure_summary JSONB;
