CREATE SCHEMA IF NOT EXISTS mcp_monitoring;
CREATE TABLE IF NOT EXISTS mcp_monitoring.tool_calls (
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
CREATE INDEX IF NOT EXISTS tool_calls_by_time ON mcp_monitoring.tool_calls (finished_at);
CREATE INDEX IF NOT EXISTS tool_calls_by_actor ON mcp_monitoring.tool_calls (actor_id, finished_at);
CREATE INDEX IF NOT EXISTS tool_calls_by_pat ON mcp_monitoring.tool_calls (pat_id, finished_at) WHERE pat_id IS NOT NULL;

-- Each API process publishes its committed coverage independently. A restarted
-- process receives a new writer id; history survives PAT/user deletion.
CREATE TABLE IF NOT EXISTS mcp_monitoring.writers (
    id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL,
    flushed_through timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    stopped boolean NOT NULL,
    dropped bigint NOT NULL,
    collecting boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS mcp_monitoring.gaps (
    writer_id uuid NOT NULL REFERENCES mcp_monitoring.writers ON DELETE CASCADE,
    since timestamptz NOT NULL,
    until_at timestamptz NOT NULL,
    PRIMARY KEY (writer_id, since)
);
-- Additive DDL also adopts earlier installations with calls already recorded.
ALTER TABLE mcp_monitoring.tool_calls
    ADD COLUMN IF NOT EXISTS trace_id uuid,
    ADD COLUMN IF NOT EXISTS span_id bytea CONSTRAINT mcp_trace_reference CHECK (
        (trace_id IS NULL AND span_id IS NULL) OR
        (trace_id IS NOT NULL AND span_id IS NOT NULL AND octet_length(span_id) = 8)
    ),
    ADD COLUMN IF NOT EXISTS search_returned integer CHECK (search_returned >= 0),
    ADD COLUMN IF NOT EXISTS browse_page_size integer CHECK (browse_page_size BETWEEN 1 AND 200),
    ADD COLUMN IF NOT EXISTS browse_depth integer CHECK (browse_depth >= -1),
    ADD COLUMN IF NOT EXISTS browse_followup boolean,
    ADD COLUMN IF NOT EXISTS usage jsonb,
    ADD COLUMN IF NOT EXISTS started_at timestamptz,
    ADD COLUMN IF NOT EXISTS request_trace_id uuid,
    ADD COLUMN IF NOT EXISTS agent_turn_id uuid,
    ADD COLUMN IF NOT EXISTS conversation_key uuid,
    ADD COLUMN IF NOT EXISTS conversation_agent varchar(128)
        CHECK ((conversation_key IS NULL) = (conversation_agent IS NULL));

CREATE TABLE IF NOT EXISTS mcp_monitoring.metric_definitions (
    tool text NOT NULL,
    key text NOT NULL,
    descriptor jsonb NOT NULL,
    PRIMARY KEY (tool, key)
);

-- Older writers can still flush the dedicated search and browse columns.
INSERT INTO mcp_monitoring.metric_definitions (tool, key, descriptor) VALUES
    ('akb_search','empty_results','{"kind":"zero_rate","label":"Empty search results","dimensions":[]}'),
    ('akb_browse','latency','{"kind":"latency","label":"Browse latency","dimensions":[
        {"key":"page_size","label":"Page size","labels":{"null":"Legacy"}},
        {"key":"depth","label":"Depth","labels":{"-1":"All (−1)"}},
        {"key":"page","label":"Page position","labels":{"false":"First","true":"Subsequent"}}]}')
ON CONFLICT (tool, key) DO NOTHING;

CREATE OR REPLACE VIEW mcp_monitoring.measured_calls AS
    SELECT observation_id, finished_at, actor_id, account_kind, auth_kind, pat_id,
           tool, outcome, duration_ms, trace_id, span_id, search_returned,
           browse_page_size, browse_depth, browse_followup, usage,
           coalesce(usage, CASE
               WHEN tool = 'akb_search' AND search_returned IS NOT NULL
                   THEN jsonb_build_object('empty_results', search_returned = 0)
               WHEN tool = 'akb_browse' AND browse_followup IS NOT NULL
                   THEN jsonb_build_object('latency', jsonb_build_object(
                       'page_size', browse_page_size, 'depth', browse_depth, 'page', browse_followup))
           END) AS samples
    FROM mcp_monitoring.tool_calls;

REVOKE ALL ON SCHEMA mcp_monitoring FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA mcp_monitoring FROM PUBLIC;
