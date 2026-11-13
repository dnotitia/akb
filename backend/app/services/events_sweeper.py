"""Background worker: delete `events` outbox rows past their retention period.

The sweep ran inside the Redis publisher, which starts only when `redis_url`
is set, so without Redis no event was ever deleted and the table grew without
bound. It now has a runner of its own, started wherever the durable workers
run (`lifecycle.start_workers`), with or without Redis.

An event is kept for `event_retention_days`, counted:

- with `redis_url` set, from its publication to the stream. An event that has
  not been published is kept, as before.
- without it, where nothing publishes, from when it occurred.

A pass reads the events in id order from the oldest, one batch per step, and
goes on while they are old: occurred, or published, more than the period ago.
Each batch's statement deletes its old events that are past retention, all of
them without Redis and the ones published that long ago with it, and leaves
the rest. The pass ends at the first event that is not old. Without Redis an
expired event behind that one waits for it, even when its own transaction
began earlier, so it is kept a little longer than the period, never less.
With Redis no event past retention is behind it: an event is published after
its id is drawn, ids are drawn in order (migration 133 sets the id sequence's
cache to 1), and that one occurred inside the period. A backlog is therefore
deleted in statements of at most one batch each, every one its own
transaction, and a pass with nothing to delete reads one batch.

Every delete fires the `events` delete trigger (migration 133), which raises
each affected Vault's retention watermark in the same statement, so an event
tail cursor below the watermark is refused with 410 and resumes at it. A
batch locks its events with SKIP LOCKED before it deletes them, so a sweep in
another process, or any other delete of the same events, never makes it wait
for an event row: a row another statement holds is left to that statement.
The trigger then takes the Vaults' marks in Vault id order, as every writer
of marks does, and nothing that holds a mark waits for an event row, so the
wait it can have for a mark closes no cycle.
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

# Without Redis: the events of a range of ids that occurred more than the
# period ago.
_DELETE_EXPIRED = """
    WITH locked AS MATERIALIZED (
        SELECT id
          FROM events
         WHERE id > $1
           AND id <= $2
           AND occurred_at < NOW() - make_interval(days => $3)
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

# With Redis: the events of a range of ids published to the stream more than
# the period ago.
_DELETE_PUBLISHED = """
    WITH locked AS MATERIALIZED (
        SELECT id
          FROM events
         WHERE id > $1
           AND id <= $2
           AND redis_published_at IS NOT NULL
           AND redis_published_at < NOW() - make_interval(days => $3)
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

# The highest event id the pass in progress has passed; 0 when no pass is in
# progress.
_position = 0


async def _sweep_batch(conn, after: int) -> tuple[int, int, int]:
    """Read the batch after ``after`` and delete its old events past retention.

    Both statements lock their rows with SKIP LOCKED, in one transaction each.
    Returns how many old events lead the batch, up to the first that is not
    old (0 once the pass is over), how many of them the statement deleted, and
    the position the pass goes on from.
    """
    days = settings.event_retention_days
    batch = await conn.fetch(_READ_BATCH, after, BATCH_SIZE, days)
    old = 0
    for row in batch:
        if not row["old"]:
            break
        old += 1
    if not old:
        return 0, 0, 0
    through = batch[old - 1]["id"]
    statement = _DELETE_PUBLISHED if settings.redis_url else _DELETE_EXPIRED
    deleted = int(await conn.fetchval(statement, after, through, days) or 0)
    return old, deleted, through


async def sweep_once() -> int:
    """Take one step of the pass: one batch.

    Returns how many old events the batch held, deleted or left (unpublished,
    or held by another statement), and the runner then takes the next step at
    once; 0 once the pass is over, and the runner waits SWEEP_INTERVAL_SECONDS
    before the next one.
    """
    global _position
    pool = await get_pool()
    async with pool.acquire() as conn:
        # A step that fails starts the next pass from the oldest event.
        after, _position = _position, 0
        old, deleted, _position = await _sweep_batch(conn, after)
    if deleted:
        logger.info("events sweep: purged %d rows", deleted)
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
