-- Target preparation is HOST-only. This table records adoption after the
-- verifier switch; global standalone receipts remain immutable source history.
ALTER TABLE users ADD COLUMN IF NOT EXISTS sso_recovery_target_id UUID;
DROP INDEX IF EXISTS users_one_recovery_admin_per_provider;
CREATE UNIQUE INDEX users_one_recovery_admin_per_provider
    ON users(auth_provider) WHERE is_recovery_admin AND sso_recovery_target_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_one_recovery_admin_per_target
    ON users(sso_recovery_target_id) WHERE is_recovery_admin AND sso_recovery_target_id IS NOT NULL;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='users_sso_recovery_target_check'
                   AND conrelid='users'::regclass) THEN
        ALTER TABLE users ADD CONSTRAINT users_sso_recovery_target_check CHECK (
            sso_recovery_target_id IS NULL OR
            (auth_provider='keycloak' AND is_admin AND is_recovery_admin AND account_kind='human')
        );
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS prepared_sso_target_adoptions (
    target_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL UNIQUE,
    realm_id TEXT NOT NULL,
    issuer TEXT NOT NULL,
    profile TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    preparation_sha256 TEXT NOT NULL CHECK (preparation_sha256 ~ '^[0-9a-f]{64}$'),
    akb_user_id UUID NOT NULL REFERENCES users(id),
    retirement_receipt JSONB NOT NULL CHECK (jsonb_typeof(retirement_receipt)='object'),
    host_preparation JSONB NOT NULL CHECK (jsonb_typeof(host_preparation)='object'),
    adopted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
