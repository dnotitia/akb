-- Nonces are globally unique, including after the original key is revoked or
-- the request is terminal. NULL leaves existing requests and callers intact.
ALTER TABLE sso_operation_requests ADD COLUMN IF NOT EXISTS request_nonce UUID;
ALTER TABLE sso_operation_requests ADD COLUMN IF NOT EXISTS proposal_sha256 TEXT;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'sso_operation_requests'::regclass AND conname = 'sso_operation_requests_nonce_digest_check') THEN
        ALTER TABLE sso_operation_requests ADD CONSTRAINT sso_operation_requests_nonce_digest_check
        CHECK ((request_nonce IS NULL AND proposal_sha256 IS NULL) OR
               (request_nonce IS NOT NULL AND proposal_sha256 IS NOT NULL AND proposal_sha256 ~ '^[0-9a-f]{64}$'));
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS sso_operation_requests_nonce
    ON sso_operation_requests (request_nonce) WHERE request_nonce IS NOT NULL;
CREATE INDEX IF NOT EXISTS sso_operation_requests_requester_history
    ON sso_operation_requests (requested_by, requested_token_id, created_at DESC, request_id DESC);
