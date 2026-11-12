"""Record, per Vault, the highest event id that has been deleted.

The event tail refused a cursor with 410 whenever it was more than one below
the Vault's earliest retained event. Event ids are shared by every Vault, so a
cursor taken before a Vault's first event, on an empty Vault, is usually far
below that event without having missed anything, and it was refused all the
same. The watermark records what was actually deleted, and a cursor is
refused only when it is below it.

A statement trigger on events raises the mark in every statement that
deletes, so deletions are recorded whichever code issues them, including the
sweeper of an earlier release that runs beside this one during a rolling
deployment or after a rollback.

The starting marks and the trigger are installed in one step while deletes
and inserts on events wait, so no delete falls between them. Deletions before
this migration left no record, and nothing can reconstruct them: a Vault that
still has events starts just below its earliest one, the boundary the tail
used until now, so history that may already have been swept is still refused,
and a Vault with no events starts without a mark. A later event deleted before
the migration while an earlier one was kept therefore stays unknown, as it was
before. Once the trigger exists, applying this again sets no starting mark:
one set later could claim a deletion that never happened. A TRUNCATE of events
records nothing; only the test runtime's reset uses one, and it truncates this
table and the Vaults with it.

The step needs SHARE ROW EXCLUSIVE on events, which waits for every open
transaction that wrote an event, and a lock request that waits queues every
later event insert, of any Vault, behind it. So the migration asks for the
lock with a short timeout inside a savepoint, holds nothing while it backs
off, and asks again; after a bounded number of attempts it stops with an
error that names the cause. Once it holds the lock, it waits only for one
grouped scan of events, which retention keeps small.
"""

import asyncio
from pathlib import Path

import asyncpg

LOCK_TIMEOUT = "500ms"
ATTEMPTS = 40
FIRST_BACKOFF_SECONDS = 0.25
MAX_BACKOFF_SECONDS = 2.0


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    sql = Path(__file__).with_suffix(".sql").read_text()
    backoff = FIRST_BACKOFF_SECONDS
    for attempt in range(1, ATTEMPTS + 1):
        try:
            # A savepoint inside the migration runner's transaction: a timed-out
            # attempt rolls back to it and releases everything it took.
            async with conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                await conn.execute("LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE")
                await conn.execute(sql)
            return
        except asyncpg.LockNotAvailableError:
            if attempt == ATTEMPTS:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    raise RuntimeError(
        f"migration 133 could not lock the events table in {ATTEMPTS} attempts of "
        f"{LOCK_TIMEOUT}: a long transaction keeps writing events. End it and start "
        "the server again."
    )
