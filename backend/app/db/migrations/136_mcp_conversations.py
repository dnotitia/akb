"""Retain a scoped conversation key when the calling agent provides one."""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    await conn.execute("""
        ALTER TABLE mcp_monitoring.tool_calls
            ADD COLUMN conversation_key uuid,
            ADD COLUMN conversation_agent varchar(128),
            ADD CHECK ((conversation_key IS NULL) = (conversation_agent IS NULL)) NOT VALID
    """)
