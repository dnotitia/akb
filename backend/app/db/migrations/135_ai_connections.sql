-- AI connection registry: one row per (account, AI client credential) seen at
-- the MCP boundary. `client_key` is the OAuth access token's authorized party
-- (`azp`) or the PAT's token id. No IP address and no request content.
CREATE TABLE IF NOT EXISTS ai_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    auth_kind TEXT NOT NULL CHECK (auth_kind IN ('oauth', 'pat')),
    client_key TEXT NOT NULL CHECK (char_length(client_key) BETWEEN 1 AND 255),
    -- A revoked PAT removes its connection: nothing can connect with it again.
    token_id UUID REFERENCES tokens(id) ON DELETE CASCADE,
    client_name TEXT CHECK (client_name IS NULL OR char_length(client_name) <= 128),
    client_version TEXT CHECK (client_version IS NULL OR char_length(client_version) <= 64),
    user_agent TEXT CHECK (user_agent IS NULL OR char_length(user_agent) <= 256),
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ai_connections_identity UNIQUE (user_id, auth_kind, client_key),
    CONSTRAINT ai_connections_pat_token CHECK (
        (auth_kind = 'pat' AND token_id IS NOT NULL AND client_key = token_id::text)
        OR (auth_kind = 'oauth' AND token_id IS NULL)
    ),
    CONSTRAINT ai_connections_seen_order CHECK (first_seen_at <= last_seen_at)
);
CREATE INDEX IF NOT EXISTS ai_connections_user_recent
    ON ai_connections (user_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS ai_connections_token
    ON ai_connections (token_id) WHERE token_id IS NOT NULL;
