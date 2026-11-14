"""Assign each Vault's event ids in commit order.

The SSE event tail reads ``events`` with ``id > cursor`` and moves its cursor
to the last id it read. ``events.id`` used to be drawn when a row was inserted,
while the row became visible only at commit. When two transactions wrote events
for the same Vault and the one with the larger id committed first, a reader
could deliver the larger id, move its cursor past the smaller one, and never
read the smaller event once it committed.

A ``BEFORE INSERT`` trigger now locks the event's Vault row in
``vault_event_order`` until the transaction ends and then draws the id. The
next event of that Vault draws its id only after every earlier one has
committed or rolled back, so within a Vault id order is commit order. Events
without a Vault take no lock. The trigger draws every id and the column keeps
no default, so an event uses one sequence value; an INSERT that names no id,
as earlier releases do, still works. A trigger on ``vaults`` creates a Vault's
row with the Vault and deletes it with the Vault, and the migration creates
the rows of existing Vaults. Event-writing transactions of one Vault run one
at a time from taking the lock to their commit. Services insert the event as
their last locking statement to keep that span short; Bare Git document writes
lock the rows they change later and then take the lock
(``akb_lock_vault_events``) before their Git commit, because the commit does
not roll back with the transaction. The wait for the lock ends after 5 seconds
with SQLSTATE 55P03 and the constraint name ``akb_event_lock_timeout``.

Dynamic tables' row-change events (migration 086) are queued by their
statement trigger and inserted when the transaction commits, after every lock
the transaction takes; a cascading delete would otherwise hold the Vault's
lock between cascade levels.

The migration leaves the notification trigger from migration 015 as it is, so
it applies in either order with a migration that replaces that function.

The changes need ACCESS EXCLUSIVE on ``events``, and the trigger on ``vaults``
needs SHARE ROW EXCLUSIVE on it. A lock request that waits queues every later
conflicting request behind it, so the migration asks for its locks with a
short timeout, holds nothing while it backs off, and asks again; it gives up
with an error after a bounded number of attempts.
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
                await conn.execute("LOCK TABLE public.events IN ACCESS EXCLUSIVE MODE")
                await conn.execute(sql)
            return
        except asyncpg.LockNotAvailableError:
            if attempt == ATTEMPTS:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    raise RuntimeError(
        f"migration 132 could not lock the events table in {ATTEMPTS} attempts of "
        f"{LOCK_TIMEOUT}: a long transaction keeps writing events. End it and start "
        "the server again."
    )
