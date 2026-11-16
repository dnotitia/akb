"""Store typed metric samples and preserve the meaning of retained observations."""

async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    await conn.execute("""
        ALTER TABLE mcp_monitoring.tool_calls ADD COLUMN usage jsonb;
        CREATE TABLE mcp_monitoring.metric_definitions (
            tool text NOT NULL, key text NOT NULL, descriptor jsonb NOT NULL, PRIMARY KEY (tool,key)
        );
        REVOKE ALL ON mcp_monitoring.metric_definitions FROM PUBLIC;
        INSERT INTO mcp_monitoring.metric_definitions VALUES
            ('akb_search','empty_results','{"kind":"zero_rate","label":"Empty search results","dimensions":[]}'),
            ('akb_browse','latency','{"kind":"latency","label":"Browse latency","dimensions":[
                {"key":"page_size","label":"Page size","labels":{"null":"Legacy"}},
                {"key":"depth","label":"Depth","labels":{"-1":"All (−1)"}},
                {"key":"page","label":"Page position","labels":{"false":"First","true":"Subsequent"}}]}');
        -- Normalize older rows and completions arriving from a rolling upgrade in place on read.
        CREATE VIEW mcp_monitoring.measured_calls AS
            SELECT tool_calls.*, coalesce(usage, CASE
                WHEN tool='akb_search' AND search_returned IS NOT NULL
                    THEN jsonb_build_object('empty_results',search_returned=0)
                WHEN tool='akb_browse' AND browse_followup IS NOT NULL
                    THEN jsonb_build_object('latency',jsonb_build_object(
                        'page_size',browse_page_size,'depth',browse_depth,'page',browse_followup))
            END) AS samples FROM mcp_monitoring.tool_calls;
        REVOKE ALL ON mcp_monitoring.measured_calls FROM PUBLIC;
    """)
