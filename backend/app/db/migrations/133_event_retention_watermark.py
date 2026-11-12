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
deployment or after a rollback. A TRUNCATE of events records nothing; only
the test runtime's reset uses one, and it truncates this table and the Vaults
with it.

This migration installs the trigger and records the backfill boundary while
inserts and deletes on events wait; migration 134 then sets the starting
marks from the events at or below the boundary, without that lock. The
boundary is the last id the events id sequence had handed out: once the lock
is granted no write to events is under way, so every committed event has an
id at or below it and every later event draws a larger one. Nothing here
reads events, so the lock lasts as long however many rows the table holds,
live or deleted and not yet cleaned up; reading the highest id from the
primary key instead walks every dead entry at its end. This needs the ids to
come from the sequence the column owns, by its default or by a trigger. A
sequence cache above 1 lets a session hand out, after the install, ids it
drew ahead before it, at or below the boundary; so under the same lock the
install sets the cache to 1 first, which makes every session drop the rest of
its block at its next draw, and then reads the boundary. The cache stays 1.

Installing needs SHARE ROW EXCLUSIVE on events, which waits for every open
transaction that wrote an event, and a lock request that waits queues every
later event insert, of any Vault, behind it. So the migration first looks for
the trigger and, when it is there, does nothing and asks for no lock.
Otherwise it asks for the lock with a short timeout inside a savepoint, holds
nothing while it backs off, and asks again; after a bounded number of attempts
it stops with an error that names the cause. Under the lock the SQL looks for
the trigger again, in case another session installed it meanwhile.
"""

import asyncio
from pathlib import Path

import asyncpg

LOCK_TIMEOUT = "500ms"
ATTEMPTS = 40
FIRST_BACKOFF_SECONDS = 0.25
MAX_BACKOFF_SECONDS = 2.0
TRIGGER_INSTALLED = """
    SELECT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = to_regclass('events') AND tgname = 'akb_events_record_deletions'
    )
"""


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    if await conn.fetchval(TRIGGER_INSTALLED):
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
