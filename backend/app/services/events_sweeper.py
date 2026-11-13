"""Background worker: delete `events` outbox rows past their retention period.

The sweep ran inside the Redis publisher, which starts only when `redis_url`
is set, so without Redis no event was ever deleted and the table grew without
bound. It now has a runner of its own, started wherever the durable workers
run (`lifecycle.start_workers`), with or without Redis.

An event is kept for `event_retention_days`, counted:

- with `redis_url` set, from when the publisher records it as published
  (`redis_published_at`): when it writes the event to the stream, or marks it
  published without writing it, as it does the gardener's own bookkeeping
  events. An event that is not recorded as published is kept, as before.
- without it, where nothing publishes, from when it occurred.

A pass reads the events in id order from the oldest, one batch per step, and
goes on while they are old: occurred, or recorded as published, more than the
period ago. Each batch's statement is given the ids of the batch's old events
and deletes those past retention, all of them without Redis and the ones
published that long ago with it, and leaves the rest. The pass ends at the
first event that is not old. Without Redis an expired event behind that one
waits for it, even when its own transaction began earlier, so it is kept
longer than the period, never less: until the event ahead of it is old too.
With Redis, of the ids drawn since
migration 133 set the id sequence's cache to 1, none past retention is behind
it: an event is published after its id is drawn, those ids are drawn in
order, and that one occurred inside the period. Ids drawn before migration
133, with a larger cache, may be out of order, and an event past retention
behind it then waits for it too. Either way the statement checks each event's
own time, so a pass ending early only delays a deletion, never makes one
early. A backlog is therefore deleted in statements of at most one batch
each, every one its own transaction, as far as a pass can reach: an event
another statement holds, or one behind an event that is not old, waits for a
later pass. A pass whose first event is not old reads one batch.

Workers that disagree about `redis_url`, as during a change of it, each sweep
by their own rule: one without it deletes events that occurred more than the
period ago, recorded as published or not, and one with it keeps the
unpublished ones. As an event is published after it occurred, neither
deletes one before the period has passed since it occurred; an event no
publisher has written yet can, however, be deleted before one does, so give
every worker the same setting.

Every delete fires the `events` delete trigger (migration 133), which raises
each affected Vault's retention watermark in the same statement, so an event
tail cursor below the watermark is refused with 410 and resumes at it. A
batch locks its events with SKIP LOCKED before it deletes them, so a sweep
never waits for an event row another statement holds: the row is left to that
statement. The trigger then takes the Vaults' marks in Vault id order, as
every writer of marks does, and a single statement that deletes events holds
its event rows before it takes any mark: a sweep therefore cannot deadlock
with another sweep, or with another statement that deletes events on its own.
A transaction that deletes events in more than one statement still can: once
its first statement holds a Vault's mark, its next one may wait for an event
row a sweep holds while the sweep waits for that mark.

The marks have no foreign key to vaults. A Vault's delete removes its mark,
but an ordinary delete leaves the Vault's events behind, and sweeping them
later writes a mark for a Vault that is gone. It is inert, as Vault ids are
never reused, yet it would stay for good. The step that ends a pass removes
the marks of Vaults that no longer exist and have no event left; while an
event of the Vault id remains, its mark stays, as the line that event's 410 is
drawn by. That delete locks the marks in Vault id order with SKIP LOCKED,
leaving a mark another statement holds for a later pass, so it waits for no
row lock and closes no cycle.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner

logger = logging.getLogger("akb.events_sweeper")

# How long the sweeper waits after a pass that found nothing to delete.
SWEEP_INTERVAL_SECONDS = 3600
# Events one step reads, and so the most one statement deletes. The trigger
# holds each statement's deleted rows in a transition table, which a small
# batch keeps in memory.
BATCH_SIZE = 1000

# The next batch of events after the pass's position, in id order, each with
# whether it is old: occurred, or published, more than the period ago.
_READ_BATCH = """
    SELECT id,
           occurred_at < NOW() - make_interval(days => $3)
           OR COALESCE(redis_published_at < NOW() - make_interval(days => $3), FALSE) AS old
      FROM events
     WHERE id > $1
     ORDER BY id
     LIMIT $2
"""

# Without Redis: the events, of the ids a batch read, that occurred more than
# the period ago. The ids, not their range: the delete is a statement of its
# own, and an event committed after the read with an id among them is not
# the batch's to delete.
_DELETE_EXPIRED = """
    WITH locked AS MATERIALIZED (
        SELECT id
          FROM events
         WHERE id = ANY($1::bigint[])
           AND occurred_at < NOW() - make_interval(days => $2)
         ORDER BY id
           FOR UPDATE SKIP LOCKED
    ),
    d AS (
        DELETE FROM events e
         USING locked
         WHERE e.id = locked.id
        RETURNING 1
    )
    SELECT COUNT(*) FROM d
"""

# With Redis: the events, of the ids a batch read, published more than the
# period ago.
_DELETE_PUBLISHED = """
    WITH locked AS MATERIALIZED (
        SELECT id
          FROM events
         WHERE id = ANY($1::bigint[])
           AND redis_published_at IS NOT NULL
           AND redis_published_at < NOW() - make_interval(days => $2)
         ORDER BY id
           FOR UPDATE SKIP LOCKED
    ),
    d AS (
        DELETE FROM events e
         USING locked
         WHERE e.id = locked.id
        RETURNING 1
    )
    SELECT COUNT(*) FROM d
"""

# Marks of Vaults that are gone, at most this many per statement.
MARK_BATCH_SIZE = 1000

# The marks of Vaults that no longer exist and have no event left, in Vault
# id order, the order every writer of marks takes them in. Each is locked
# first and skipped if another statement holds it, so the delete never waits.
_DELETE_ORPHANED_MARKS = """
    WITH orphaned AS MATERIALIZED (
        SELECT w.vault_id
          FROM event_retention_watermarks w
         WHERE NOT EXISTS (SELECT 1 FROM vaults v WHERE v.id = w.vault_id)
           AND NOT EXISTS (SELECT 1 FROM events e WHERE e.vault_id = w.vault_id)
         ORDER BY w.vault_id
         LIMIT $1
           FOR UPDATE OF w SKIP LOCKED
    ),
    d AS (
        DELETE FROM event_retention_watermarks w
         USING orphaned
         WHERE w.vault_id = orphaned.vault_id
        RETURNING 1
    )
    SELECT COUNT(*) FROM d
"""

# The highest event id the pass in progress has passed; 0 when no pass is in
# progress.
_position = 0


async def _sweep_batch(conn, after: int, delete: str) -> tuple[int, int, int]:
    """Read the batch after ``after`` and delete its old events past retention.

    ``delete`` is the statement for the configured transport, `_DELETE_EXPIRED`
    or `_DELETE_PUBLISHED`. It is given the ids of the old events the batch
    read, so it deletes at most a batch, and locks them with SKIP LOCKED, in a
    transaction of its own. Returns how many old events lead the batch, up to
    the first that is not old (0 once the pass is over), how many of them the
    statement deleted, and the position the pass goes on from.
    """
    days = settings.event_retention_days
    batch = await conn.fetch(_READ_BATCH, after, BATCH_SIZE, days)
    old: list[int] = []
    for row in batch:
        if not row["old"]:
            break
        old.append(row["id"])
    if not old:
        return 0, 0, 0
    deleted = int(await conn.fetchval(delete, old, days) or 0)
    return len(old), deleted, old[-1]


async def _delete_orphaned_marks(conn) -> int:
    """Delete one batch of the marks of Vaults that are gone and have no event left."""
    return int(await conn.fetchval(_DELETE_ORPHANED_MARKS, MARK_BATCH_SIZE) or 0)


async def sweep_once() -> int:
    """Take one step of the pass: one batch, or, once no old event is left, the marks.

    Returns the number of old events the batch held, deleted or left
    (unpublished, or held by another statement), and the runner then takes
    the next step at once. The step that finds no old event left ends the
    pass: it deletes the marks of Vaults that are gone and returns 0, and the
    runner waits SWEEP_INTERVAL_SECONDS before the next pass.
    """
    global _position
    # A step that fails, before it has a connection, while it holds one, or
    # as it gives it back, starts the next pass from the oldest event: the
    # position it reached is kept only once the connection is back.
    after, _position = _position, 0
    delete = _DELETE_PUBLISHED if settings.redis_url else _DELETE_EXPIRED
    removed = 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        old, deleted, position = await _sweep_batch(conn, after, delete)
        if not old:
            while True:
                batch = await _delete_orphaned_marks(conn)
                removed += batch
                if batch < MARK_BATCH_SIZE:
                    break
    _position = position
    if deleted:
        logger.info("events sweep: purged %d rows", deleted)
    if removed:
        logger.info("events sweep: removed the retention marks of %d deleted Vaults", removed)
    return old


_runner = BackfillRunner(
    "events_sweeper",
    sweep_once,
    idle_secs=SWEEP_INTERVAL_SECONDS,
    # Each step logs what it deleted; the runner's own per-step line would
    # repeat it.
    log_progress=False,
)
start = _runner.start


async def stop() -> None:
    await _runner.stop()
