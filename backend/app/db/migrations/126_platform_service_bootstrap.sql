-- Permanent issuance intent, independent of deletable user/token rows and audit retention.
CREATE TABLE IF NOT EXISTS platform_service_bootstrap (
    purpose TEXT PRIMARY KEY CHECK (purpose = 'platform-service-v1'),
    operation_id UUID NOT NULL UNIQUE,
    org_id UUID NOT NULL,
    tenant_slug TEXT NOT NULL,
    namespace TEXT NOT NULL,
    owner_uid UUID NOT NULL,
    user_id UUID NOT NULL UNIQUE,
    token_id UUID NOT NULL UNIQUE,
    intent_sha256 TEXT NOT NULL CHECK (intent_sha256 ~ '^[a-f0-9]{64}$'),
    credential_sha256 TEXT NOT NULL CHECK (credential_sha256 ~ '^[a-f0-9]{64}$'),
    runtime_identity JSONB NOT NULL,
    service_authority_client_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
