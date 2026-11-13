"""Background worker: delete `events` outbox rows past their retention period.

The sweep ran inside the Redis publisher, which starts only when `redis_url`
is set, so without Redis no event was ever deleted and the table grew without
bound. It now has a runner of its own, started wherever the durable workers
run (`lifecycle.start_workers`), with or without Redis.

An event is kept for `event_retention_days`, counted:

- with `redis_url` set, from its publication to the stream. An event that has
  not been published is kept, as before.
- without it, where nothing publishes, from when it occurred. The sweep reads
  the events in id order from the oldest, a batch at a time, and deletes them
  up to the first one still inside the period, where the pass ends. An event
  behind that one waits for it, even when its own transaction began earlier,
  so it is kept a little longer than the period, never less. A backlog is
  deleted in short statements, and a pass with nothing to delete reads one
  batch at most.

Every delete fires the `events` delete trigger (migration 133), which raises
each affected Vault's retention watermark in the same statement, so an event
tail cursor below the watermark is refused with 410 and resumes at it.
Without Redis a batch locks its events with SKIP LOCKED before it deletes
them, so a sweep in another process, or another delete of the same events,
never makes it wait for an event row. Its trigger then takes the Vaults' marks
in Vault id order, as every writer of marks does, and nothing that holds a
mark waits for an event row, so the wait it can have for a mark closes no
cycle.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner

logger = logging.getLogger("akb.events_sweeper")

# How long the sweeper waits after a pass that found nothing to delete.
SWEEP_INTERVAL_SECONDS = 3600
# Events one statement deletes without Redis. The trigger holds each
# statement's deleted rows in a transition table, which a small batch keeps in
# memory.
BATCH_SIZE = 1000

# With Redis: the events published to the stream more than the period ago, in
# one statement, as the publisher's sweep deleted them.
_DELETE_PUBLISHED = """
    WITH d AS (
        DELETE FROM events
         WHERE redis_published_at IS NOT NULL
           AND redis_published_at < NOW() - make_interval(days => $1)
        RETURNING 1
    )
    SELECT COUNT(*) FROM d
"""

# Without Redis: the next batch of events after the pass's position, in id
# order, each with whether it occurred more than the period ago.
_READ_BATCH = """
    SELECT id, occurred_at < NOW() - make_interval(days => $3) AS expired
      FROM events
     WHERE id > $1
     ORDER BY id
     LIMIT $2
"""

# Without Redis: the events of a range of ids that occurred more than the
# period ago. Each is locked first and skipped if another statement holds it,
# so the delete never waits for an event row; the trigger then raises the
# marks of their Vaults.
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

# The highest event id the pass in progress without Redis has passed; 0 when
# no pass is in progress.
_position = 0


async def _delete_published(conn) -> int:
    """With Redis: delete the events published more than the period ago."""
    return int(await conn.fetchval(_DELETE_PUBLISHED, settings.event_retention_days) or 0)


async def _delete_expired(conn, after: int) -> tuple[int, int]:
    """Without Redis: delete the next batch of events past the period.

    Reads the batch after ``after`` and deletes its events up to the first one
    still inside the period. Returns how many it deleted and the position the
    pass goes on from, 0 when the pass has ended: at an event inside the
    period, at the newest event, or at a batch whose rows other statements
    held, which they are deleting.
    """
    days = settings.event_retention_days
    batch = await conn.fetch(_READ_BATCH, after, BATCH_SIZE, days)
    expired = 0
    for row in batch:
        if not row["expired"]:
            break
        expired += 1
    if not expired:
        return 0, 0
    through = batch[expired - 1]["id"]
    deleted = int(await conn.fetchval(_DELETE_EXPIRED, after, through, days) or 0)
    if deleted and expired == len(batch) == BATCH_SIZE:
        return deleted, through
    return deleted, 0


async def sweep_once() -> int:
    """Take one step of the sweep; returns how many events it deleted.

    A step that deleted something makes the runner take the next one at once,
    so a backlog is deleted in steady steps; a step that deleted nothing ends
    the pass, and the runner waits SWEEP_INTERVAL_SECONDS before the next.
    """
    global _position
    pool = await get_pool()
    async with pool.acquire() as conn:
        if settings.redis_url:
            deleted = await _delete_published(conn)
        else:
            # A step that fails starts the next pass from the oldest event.
            after, _position = _position, 0
            deleted, _position = await _delete_expired(conn, after)
    if deleted:
        logger.info("events sweep: purged %d rows", deleted)
    return deleted


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
