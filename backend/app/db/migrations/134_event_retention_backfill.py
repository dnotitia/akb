"""Set the starting retention watermarks, after migration 133's trigger is in place.

Deletions before migration 133 left no record, and nothing can reconstruct
them. A Vault that still had events when the trigger was installed starts
just below its earliest one, the boundary the tail used until now, so history
that may already have been swept is still refused; a later event deleted while
an earlier one was kept stays unknown, as it was before. A Vault with no
events then starts without a mark, as it had no boundary before.

This runs in its own transaction, after migration 133's has committed, and
reads events without the lock that stops writes, so its scan, however long,
holds up no event insert. It is correct without that lock:

- Every mark that exists when this runs was written by the trigger, in a
  delete committed after the install, and is exact: the highest id of the
  Vault deleted since. Such a mark is already at or above the starting mark
  the Vault would get. Every event of the Vault deleted since the install was
  at or above its earliest event at install time, because the events present
  then were and every later one is above the boundary. So the mark is left
  alone (ON CONFLICT DO NOTHING): raising it to just below the earliest event
  left would claim the ids between, which other Vaults' events use, as
  deleted, and refuse a cursor that missed nothing.
- A Vault with no mark had no event deleted since the install, so the
  earliest of its events at or below the boundary is still its earliest at
  install time, and its starting mark is the old boundary.
- Only events at or below the boundary count, the last id handed out when
  the trigger was installed. A Vault whose events all came later, such as
  one that was empty then, gets no mark from here, so a cursor taken while it
  was empty is not refused.
- A Vault deleted meanwhile gets no mark. The marks are computed first,
  without locks, and then the Vaults they name, and only those, are read
  FOR KEY SHARE in the same statement: a delete under way is waited for, and
  a Vault it removed is not returned, while a delete that starts later waits
  for this to commit and then takes the mark with the Vault, by cascade.
  Read from the statement's snapshot alone, a Vault whose delete committed
  after the snapshot failed the mark's foreign key check, and the migration
  with it, which stopped the server's start. A Vault with no mark to set is
  not locked, so a long transaction on its row, such as a delete, holds up
  nothing here.
- A delete racing this statement is recorded either way. If its trigger
  wrote the Vault's row first, committed or not, the insert here waits for
  it and then does nothing; ON CONFLICT sees the newest committed row even
  when this statement's snapshot does not. If this statement wrote the row
  first, the trigger waits for it to commit and raises it with GREATEST to the
  deleted id, which is above the starting mark: the deleted event was at or
  above the earliest one this statement saw.

So every event deleted after migration 133 is at or below its Vault's mark,
no mark claims a deletion that did not happen since the install, and an
earlier deletion is covered as far as the boundary the tail used before. The
boundary row is deleted in the same statement, so running this again sets
nothing: a second run would put a mark just below whatever events later
deletions had left, and claim deletions that never happened.
"""

from pathlib import Path


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as connection:
            await migrate(connection)
        return
    await conn.execute(Path(__file__).with_suffix(".sql").read_text())
