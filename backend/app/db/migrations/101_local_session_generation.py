"""Fence local JWT issuance/revocation independently of wall-clock ordering."""

SCHEMA = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_generation BIGINT
    NOT NULL DEFAULT 0 CHECK (session_generation >= 0);

-- Old application processes only write the cutoff. Fence those revocations
-- too, including equal/backdated NOW() values from concurrent transactions.
-- New processes explicitly advance the generation, so do not increment twice.
CREATE OR REPLACE FUNCTION fence_local_session_cutoff() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.session_generation = OLD.session_generation THEN
        NEW.session_generation := OLD.session_generation + 1;
    END IF;
    NEW.tokens_revoked_before := GREATEST(OLD.tokens_revoked_before, NEW.tokens_revoked_before);
    RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER users_local_session_cutoff_fence
    BEFORE UPDATE OF tokens_revoked_before ON users
    FOR EACH ROW EXECUTE FUNCTION fence_local_session_cutoff();
"""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA)
    else:
        await conn.execute(SCHEMA)
