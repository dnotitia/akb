"""Keep schema-derived diagnostics for new tool failures."""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    await conn.execute("ALTER TABLE mcp_monitoring.tool_calls ADD COLUMN IF NOT EXISTS error_diagnostic varchar(256)")
