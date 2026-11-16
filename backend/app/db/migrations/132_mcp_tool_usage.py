"""Keep missing measurements distinct from empty results and first pages."""

async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    await conn.execute("""
        ALTER TABLE mcp_monitoring.tool_calls
            ADD COLUMN search_returned integer CHECK (search_returned >= 0),
            ADD COLUMN browse_page_size integer CHECK (browse_page_size BETWEEN 1 AND 200),
            ADD COLUMN browse_depth integer CHECK (browse_depth >= -1),
            ADD COLUMN browse_followup boolean
    """)
