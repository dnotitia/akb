CREATE SCHEMA mcp_monitoring;
CREATE TABLE mcp_monitoring.tool_calls (
    observation_id uuid PRIMARY KEY,
    finished_at timestamptz NOT NULL,
    actor_id uuid NOT NULL,
    account_kind text NOT NULL CHECK (account_kind IN ('human', 'service')),
    auth_kind text NOT NULL CHECK (auth_kind IN ('oauth', 'pat', 'service')),
    pat_id uuid,
    tool text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('ok', 'tool_error', 'rpc_error', 'cancelled', 'unknown')),
    duration_ms integer NOT NULL CHECK (duration_ms >= 0),
    CHECK ((auth_kind = 'pat') = (pat_id IS NOT NULL))
);
CREATE INDEX tool_calls_by_time ON mcp_monitoring.tool_calls (finished_at);
CREATE INDEX tool_calls_by_actor ON mcp_monitoring.tool_calls (actor_id, finished_at);
CREATE INDEX tool_calls_by_pat ON mcp_monitoring.tool_calls (pat_id, finished_at) WHERE pat_id IS NOT NULL;

-- Each API process publishes its committed coverage independently. A restarted
-- process receives a new writer id; history survives PAT/user deletion.
CREATE TABLE mcp_monitoring.writers (
    id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL,
    flushed_through timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    stopped boolean NOT NULL,
    dropped bigint NOT NULL,
    collecting boolean NOT NULL DEFAULT true
);
CREATE TABLE mcp_monitoring.gaps (
    writer_id uuid NOT NULL REFERENCES mcp_monitoring.writers ON DELETE CASCADE,
    since timestamptz NOT NULL,
    until_at timestamptz NOT NULL,
    PRIMARY KEY (writer_id, since)
);
REVOKE ALL ON SCHEMA mcp_monitoring FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA mcp_monitoring FROM PUBLIC;
