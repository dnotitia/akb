-- Browser challenge possession is not evidence. Only the verified callback's
-- successful session transaction may consume it and write an event.
CREATE TABLE IF NOT EXISTS sso_operation_login_challenges (
    challenge_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL REFERENCES sso_identity_operations(operation_id),
    akb_user_id UUID NOT NULL,
    credential_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS sso_callback_login_events (
    event_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL REFERENCES sso_identity_operations(operation_id),
    challenge_id UUID NOT NULL UNIQUE REFERENCES sso_operation_login_challenges(challenge_id),
    installation_id UUID NOT NULL,
    target_id UUID NOT NULL,
    akb_user_id UUID NOT NULL,
    issuer TEXT NOT NULL,
    provider_alias TEXT NOT NULL,
    subject TEXT NOT NULL,
    runtime_generation BIGINT NOT NULL,
    sso_session_epoch UUID NOT NULL,
    source_revision TEXT,
    cohort_sha256 TEXT NOT NULL,
    provider_revision BIGINT,
    provider_secret_revision TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
