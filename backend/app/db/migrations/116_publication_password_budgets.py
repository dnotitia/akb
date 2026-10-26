"""Shared bounded password-attempt state, independent of API replica lifetime."""

from __future__ import annotations


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    if not await conn.fetchval("SELECT to_regclass('public.publications') IS NOT NULL"):
        return
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publication_password_budgets (
                publication_id UUID PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
                state JSONB NOT NULL DEFAULT '{}'::jsonb,
                CONSTRAINT publication_password_budgets_state_object
                    CHECK (jsonb_typeof(state) = 'object')
            );
            REVOKE ALL PRIVILEGES ON TABLE publication_password_budgets FROM PUBLIC;
            COMMENT ON TABLE publication_password_budgets IS
                'One password budget per protected publication; bounded IP state and quiet-hour logical expiry.';
            """
        )
