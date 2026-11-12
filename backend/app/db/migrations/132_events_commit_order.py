"""Assign each Vault's event ids in commit order.

The SSE event tail reads ``events`` with ``id > cursor`` and moves its cursor
to the last id it read. ``events.id`` used to be drawn when a row was inserted,
while the row became visible only at commit. When two transactions wrote events
for the same Vault and the one with the larger id committed first, a reader
could deliver the larger id, move its cursor past the smaller one, and never
read the smaller event once it committed.

A ``BEFORE INSERT`` trigger now takes a transaction-scoped advisory lock for the
event's Vault and draws the id again. The next event of that Vault draws its id
only after every earlier one has committed or rolled back, so within a Vault id
order is commit order. Events without a Vault are unchanged. Event-writing
transactions of one Vault therefore run one at a time from their event insert
to their commit; services write the event as their last locking statement to
keep that span short.

The migration leaves the notification trigger from migration 015 as it is, so
it applies in either order with a migration that replaces that function.
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
