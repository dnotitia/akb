-- Permanent, non-secret lineage survives account or token deletion.
CREATE TABLE IF NOT EXISTS approval_service_bootstrap (
    token_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    installation_id UUID NOT NULL,
    credential_sha256 TEXT NOT NULL CHECK (credential_sha256 ~ '^[a-f0-9]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- NULL identifies requests written before dedicated requester keys existed.
ALTER TABLE sso_operation_requests ADD COLUMN IF NOT EXISTS requester_scope TEXT;
