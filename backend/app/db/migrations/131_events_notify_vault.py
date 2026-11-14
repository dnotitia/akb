"""Name the event's Vault in the `events` insert notification.

Migration 015's `akb_events_notify()` sent only the new row's id on the
`akb_events` channel. A server process could not tell which Vault had changed,
so it woke every open event tail stream for every event, and each stream then
queried its own Vault whether or not the event was in it. The function now
sends `<vault_id>:<id>`, with an empty Vault part for an event that has none,
and a process wakes only that Vault's streams. The trigger is unchanged; it
calls the replaced function from the next insert on.

A rolling deployment needs no ordering:

- A process from an earlier release wakes every stream on any notification
  and does not read the payload, so the new format changes nothing for it.
- A process from this release applies boot migrations before it serves
  streams, so the function it listens to already sends the new format.
- Where migrations are applied by hand, a process may serve before this one
  has run. The id-only payload it receives then has no separator, and it wakes
  every stream for it, as before.
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
