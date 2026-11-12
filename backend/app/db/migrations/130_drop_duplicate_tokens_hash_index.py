"""Drop `idx_tokens_hash`, a second index on `tokens.token_hash`.

`tokens.token_hash` is declared UNIQUE, and PostgreSQL enforces that with its
own index (`tokens_token_hash_key`), which also serves every lookup of a token
by its hash. `idx_tokens_hash` was a plain btree over the same column: it
answers no query the unique index does not, and every index entry a token
write creates is written to both. init.sql no longer creates it, so
installations created from now on never have it.

The runner applies each migration in one transaction with its ledger row, and
DROP INDEX CONCURRENTLY cannot run inside a transaction block, so this is a
plain DROP INDEX. It takes an ACCESS EXCLUSIVE lock on `tokens` for as long
as the drop takes, which for a table of one row per issued token is brief.
Getting the lock can take longer: the DROP waits behind any transaction that
holds a lock on `tokens`, and PAT authentication, which updates `tokens`,
queues behind the waiting DROP, so PAT requests may stall briefly. The
runner's 5-second lock_timeout ends each wait and the runner retries the
migration.

Idempotent: IF EXISTS makes a re-run, or a database that never had the index,
a no-op.
"""

DDL = "DROP INDEX IF EXISTS idx_tokens_hash"


async def migrate(conn=None):
    if conn is not None:
        await conn.execute(DDL)
        return
    from app.db.postgres import get_pool

    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute(DDL)
