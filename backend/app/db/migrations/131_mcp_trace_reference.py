"""Link newly sampled MCP observations to their server span."""

async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    await conn.execute("""
        ALTER TABLE mcp_monitoring.tool_calls
        ADD COLUMN trace_id uuid,
        ADD COLUMN span_id bytea,
        ADD CONSTRAINT mcp_trace_reference CHECK (
            (trace_id IS NULL AND span_id IS NULL) OR
            (trace_id IS NOT NULL AND span_id IS NOT NULL AND octet_length(span_id) = 8)
        )
    """)
