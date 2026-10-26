-- An installation UUID belongs to this database, independently of revision backend.
CREATE TABLE IF NOT EXISTS sso_operation_installation (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    installation_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid()
);
INSERT INTO sso_operation_installation(singleton) VALUES(true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS sso_provider_ownership (
    alias TEXT PRIMARY KEY,
    owner_id TEXT,
    revision BIGINT NOT NULL CHECK (revision > 0),
    fingerprint TEXT NOT NULL,
    emergency_disabled BOOLEAN NOT NULL DEFAULT false,
    updated_by UUID NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sso_identity_operations (
    operation_id UUID PRIMARY KEY,
    installation_id UUID NOT NULL,
    credential_hash TEXT NOT NULL UNIQUE,
    granted_by UUID NOT NULL,
    actor_id UUID NOT NULL,
    actor_token_id UUID NOT NULL,
    provider_alias TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    provider_revision BIGINT NOT NULL,
    provider_fingerprint TEXT NOT NULL,
    current_provider_revision BIGINT NOT NULL,
    current_provider_fingerprint TEXT NOT NULL,
    provider_payload_sha256 JSONB NOT NULL,
    runtime_generation BIGINT NOT NULL,
    session_epoch UUID NOT NULL,
    cohort JSONB NOT NULL,
    cohort_sha256 TEXT NOT NULL,
    verbs TEXT[] NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    retired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > created_at AND expires_at <= created_at + INTERVAL '10 minutes')
);

CREATE TABLE IF NOT EXISTS sso_identity_operation_requests (
    operation_id UUID NOT NULL REFERENCES sso_identity_operations(operation_id),
    nonce UUID NOT NULL,
    request_sha256 TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (operation_id, nonce)
);

-- Only bindings inserted by this operation are eligible for its compensation.
CREATE TABLE IF NOT EXISTS sso_identity_operation_bindings (
    operation_id UUID NOT NULL REFERENCES sso_identity_operations(operation_id),
    item_index INTEGER NOT NULL,
    identity_id UUID NOT NULL,
    PRIMARY KEY (operation_id, item_index)
);
