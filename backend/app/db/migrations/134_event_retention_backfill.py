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

- Every deletion committed after migration 133 is recorded by the trigger,
  in the deleting statement, as a mark at least as high as the deleted id.
- The marks here come only from events at or below the boundary, the highest
  id when the trigger was installed. A Vault whose events all came later,
  such as one that was empty then, gets no mark from here, so a cursor taken
  while it was empty is not refused.
- Each mark is just below the earliest such event this statement still sees.
  A delete committed before its snapshot raised the trigger's mark already,
  and the events it removed are not seen. A delete committed after the
  snapshot has its mark merged either way: if its trigger wrote the row
  first, ON CONFLICT reads the newest committed row and GREATEST keeps the
  higher mark; if this statement wrote the row first, the trigger waits for
  it to commit and applies GREATEST itself.

So no mark is ever lowered, every event deleted after migration 133 is at or
below its Vault's mark, and an earlier deletion is covered as far as the
boundary the tail used before. The boundary row is deleted in the same
statement, so running this again sets nothing: a second run would put a mark
just below whatever events later deletions had left, and claim deletions that
never happened.
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
