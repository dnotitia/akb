"""Record, per Vault, the highest event id that retention has deleted.

The event tail refused a cursor with 410 whenever it was more than one below
the Vault's earliest retained event. Event ids are shared by every Vault, so a
cursor taken before a Vault's first event, on an empty Vault, is usually far
below that event without having missed anything, and it was refused all the
same. The watermark records what was actually deleted: the sweeper raises it
in the statement that deletes the events, and a cursor is refused only when it
is below it.

Deletions before this migration left no record. A Vault that still has events
starts with its watermark just below its earliest retained event, the boundary
the tail used until now, so history that may already have been swept is still
refused. A Vault with no retained events starts without one, as it had no
boundary before. The row goes with its Vault.
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
