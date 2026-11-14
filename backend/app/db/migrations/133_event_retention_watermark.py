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

The marks have no foreign key to vaults, and nothing that writes one reads or
locks a Vault. A delete of events holds the deleted rows when its trigger
writes the marks; had it then waited for a Vault row, as a foreign key check
or a lock on the Vault does, a Vault cleanup that locks the Vault before it
deletes the same events would deadlock with it. Vault ids are never reused,
so a mark for a Vault that no longer exists, as when its leftover events are
swept after it is deleted, is inert. A trigger on vaults removes a Vault's
mark in the statement that deletes the Vault. Lock order, which leaves no
cycle: a statement that writes marks holds its deleted event rows first and
then the mark rows, which it takes in Vault id order, as migration 134 does;
a Vault delete holds its Vault row, and any event rows its code deleted
before, and then the mark row. Nothing that holds a mark row waits for an
event row or a Vault row.

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
it stops with an error that names the table it could not lock. The trigger on
vaults needs SHARE ROW EXCLUSIVE on vaults as well. Waiting for that while
holding the events lock would hold up every event write, and a transaction
that changed a Vault and then writes its event, as a change of public access
does, would wait for this while this waited for it; so once the events lock
is held, the vaults lock is taken at once or the attempt rolls back and backs
off like any other. Under the locks the SQL looks for the trigger again, in
case another session installed it meanwhile.
"""

import asyncio
from pathlib import Path

import asyncpg

LOCK_TIMEOUT = "500ms"
ATTEMPTS = 40
FIRST_BACKOFF_SECONDS = 0.25
MAX_BACKOFF_SECONDS = 2.0
# What keeps each lock the install needs from being granted, for its error.
_BUSY = {
    "events table": "a long transaction keeps writing events",
    "vaults table": "a long transaction keeps a Vault changed",
    "events id sequence": "a long transaction keeps the events id sequence in use",
}
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
    blocked_on = "events table"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            # A savepoint inside the migration runner's transaction: a timed-out
            # attempt rolls back to it and releases everything it took.
            async with conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                blocked_on = "events table"
                await conn.execute("LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE")
                blocked_on = "vaults table"
                await conn.execute("LOCK TABLE vaults IN SHARE ROW EXCLUSIVE MODE NOWAIT")
                blocked_on = "events id sequence"
                await conn.execute(sql)
            return
        except asyncpg.LockNotAvailableError:
            if attempt == ATTEMPTS:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    raise RuntimeError(
        f"migration 133 could not lock the {blocked_on} in {ATTEMPTS} attempts: "
        f"{_BUSY[blocked_on]}. End it and start the server again."
    )
