"""Record the explicit inbound trace context independently of span sampling."""

async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    await conn.execute("""
        ALTER TABLE mcp_monitoring.tool_calls
            ADD COLUMN started_at timestamptz,
            ADD COLUMN request_trace_id uuid
    """)
