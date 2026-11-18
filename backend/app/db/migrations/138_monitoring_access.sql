ALTER TABLE users ADD COLUMN monitoring_access boolean,
    ADD CONSTRAINT monitoring_accounts_are_unprivileged CHECK (
        monitoring_access IS NULL OR (account_kind = 'human' AND NOT is_admin AND NOT is_recovery_admin)
    );

CREATE TABLE mcp_monitoring.sessions (
    token_hash text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_generation bigint NOT NULL,
    runtime_generation bigint NOT NULL,
    auth_mode text NOT NULL CHECK (auth_mode IN ('local', 'sso')),
    external_identity_id uuid REFERENCES external_identities(id) ON DELETE CASCADE,
    identity_subject text,
    keycloak_sid text,
    expires_at timestamptz NOT NULL,
    CHECK ((auth_mode = 'sso') = (external_identity_id IS NOT NULL)),
    CHECK ((auth_mode = 'sso') = (identity_subject IS NOT NULL)),
    CHECK ((auth_mode = 'sso') = (keycloak_sid IS NOT NULL))
);
CREATE INDEX monitoring_sessions_by_user ON mcp_monitoring.sessions(user_id);
CREATE INDEX monitoring_sessions_by_expiry ON mcp_monitoring.sessions(expires_at);
CREATE INDEX monitoring_sessions_by_sso_sid ON mcp_monitoring.sessions(keycloak_sid) WHERE keycloak_sid IS NOT NULL;

CREATE TABLE mcp_monitoring.pat_references (
    pat_id uuid PRIMARY KEY,
    ref uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    alias_number bigint GENERATED ALWAYS AS IDENTITY UNIQUE
);
INSERT INTO mcp_monitoring.pat_references (pat_id)
SELECT DISTINCT pat_id FROM mcp_monitoring.tool_calls WHERE pat_id IS NOT NULL ORDER BY pat_id;

CREATE FUNCTION mcp_monitoring.register_pat_reference() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    INSERT INTO mcp_monitoring.pat_references (pat_id)
    SELECT DISTINCT c.pat_id FROM new_calls c
    WHERE c.pat_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM mcp_monitoring.pat_references p WHERE p.pat_id = c.pat_id)
    ORDER BY pat_id
    ON CONFLICT (pat_id) DO NOTHING;
    RETURN NULL;
END;
$$;
CREATE TRIGGER register_pat_reference
AFTER INSERT ON mcp_monitoring.tool_calls
REFERENCING NEW TABLE AS new_calls
FOR EACH STATEMENT EXECUTE FUNCTION mcp_monitoring.register_pat_reference();
