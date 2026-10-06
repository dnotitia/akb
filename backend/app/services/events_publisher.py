"""Background worker: drain `events` outbox → Redis Streams.

Activated only when `redis_url` is configured (see `lifecycle.start_workers`).
PG remains the source of truth — `events` rows are inserted in the same
transaction as the domain change. This worker reads pending rows
(`redis_published_at IS NULL`), `XADD`s them to a Redis Stream, and
marks the row published. On Redis outage the rows accumulate and the
backoff schedule (shared with the indexing workers) keeps
retries bounded.

External consumers subscribe via `XREAD` / consumer groups:
    XREAD COUNT 100 BLOCK 5000 STREAMS akb:events $
or with a durable cursor:
    XREADGROUP GROUP my-group my-consumer COUNT 100 BLOCK 5000 \\
        STREAMS akb:events >

Why not LISTEN/NOTIFY → Redis directly: NOTIFY is fire-and-forget and
caps payloads at 8KB. The outbox sweep gives us replayability and
keeps the in-tx invariant that subscribers never see an event for a
write that rolled back.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import cast

import redis.asyncio as redis_async

from app.config import settings
from app.db.postgres import get_pool
from app.services._backfill import BackfillRunner, MAX_RETRIES, next_attempt_delay

logger = logging.getLogger("akb.events_publisher")

BATCH_SIZE = 64

# Gardener operational tables whose row-change chatter must never reach the
# event stream. The gardener writes sweep_log + gardener_event_log + gardener_kv
# on EVERY sweep, and each of those writes fires the dynamic-table
# rows_changed trigger — so without this filter the daemon's own bookkeeping
# floods akb:events (~60% measured on SKH PoC 2026-09-30) and buries the real
# source-vault changes the stream exists for.
#
# Identity is (vault NAME prefix, table NAME), resolved per event through the
# vault_tables registry — NOT the URI suffix alone. Table names are not
# reserved: a user vault may legitimately own a `sweep_log` table, and that
# table's changes must still publish. Only the gardener's own operational
# tables (gdn-* vaults, exact table names) are suppressed.
_SUPPRESSED_VAULT_PREFIX = "gdn-"
_SUPPRESSED_TABLE_NAMES = frozenset({
    "sweep_log",
    "gardener_event_log",
    "gardener_kv",
})


async def _is_suppressed(conn, row: dict) -> bool:
    """True when a rows_changed event is gardener bookkeeping, not data.

    Resolves the (vault name, table name) pair from the vault_tables registry
    via the event's vault_id + resource_uri, then checks the gdn-* prefix and
    the exact table name. A user vault's same-named table does NOT match.
    Unknown/unresolvable rows are NOT suppressed (fail-open: publish).
    """
    resource_uri = row.get("resource_uri") or ""
    vault_id = row.get("vault_id")
    if not resource_uri or vault_id is None:
        return False
    table_name = _table_name_from_uri(resource_uri)
    if table_name not in _SUPPRESSED_TABLE_NAMES:
        return False
    vault_name = await conn.fetchval(
        "SELECT name FROM vaults WHERE id = $1", vault_id,
    )
    return isinstance(vault_name, str) and vault_name.startswith(_SUPPRESSED_VAULT_PREFIX)


def _table_name_from_uri(resource_uri: str) -> str:
    """Extract the table name from akb://{vault}/table/{name} or the coll/ form."""
    marker = "/table/"
    idx = resource_uri.find(marker)
    if idx < 0:
        return ""
    return resource_uri[idx + len(marker):]

# Sweep tuning — purge rows that were successfully published more than
# 7 days ago. The grace window exists so an operator can debug delivery
# issues against the outbox after the fact; once it's expired the row
# is also gone from the stream's MAXLEN window so no reason to keep it.
SWEEP_GRACE_INTERVAL = "7 days"
SWEEP_INTERVAL_SECONDS = 3600.0
_last_sweep_at: float = 0.0


# ── Redis client (lazy, shared) ──────────────────────────────────


_redis_client: redis_async.Redis | None = None


def _build_client() -> redis_async.Redis:
    # `from_url` handles redis://, rediss://, and unix sockets uniformly.
    # decode_responses=False so XADD payload bytes round-trip cleanly.
    return redis_async.from_url(
        settings.redis_url,
        password=settings.redis_password or None,
        decode_responses=False,
        socket_keepalive=True,
        health_check_interval=30,
    )


async def _client() -> redis_async.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = _build_client()
    return _redis_client


async def close_client() -> None:
    global _redis_client
    if _redis_client is not None:
        try:
            # redis-py 5.x uses aclose() preferred but close() is the
            # backwards-compatible name available since 4.x.
            await _redis_client.aclose() if hasattr(_redis_client, "aclose") else await _redis_client.close()
        finally:
            _redis_client = None


# ── Drain pipeline ───────────────────────────────────────────────


async def _claim_batch(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        WITH pending AS (
            SELECT id
              FROM events
             WHERE redis_published_at IS NULL
               AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
               AND attempts < $2
               AND abandoned_at IS NULL
             ORDER BY next_attempt_at NULLS FIRST, id
             LIMIT $1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE events e
           SET next_attempt_at = NOW() + INTERVAL '10 minutes',
               claimed_at = NOW(),
               attempts = attempts + 1
          FROM pending p
         WHERE e.id = p.id
        RETURNING e.id, e.occurred_at, e.vault_id, e.kind,
                  e.resource_uri, e.actor_id, e.payload, e.attempts,
                  e.claimed_at
        """,
        BATCH_SIZE, MAX_RETRIES,
    )
    return [dict(r) for r in rows]


async def _mark_published(conn, event_id: int) -> None:
    await conn.execute(
        """
        UPDATE events
           SET redis_published_at = NOW(),
               attempts = 0,
               last_error = NULL,
               next_attempt_at = NULL,
               claimed_at = NULL,
               abandoned_at = NULL
         WHERE id = $1
        """,
        event_id,
    )


async def _mark_failure(conn, event_id: int, attempt_count: int, error: str) -> None:
    terminal = attempt_count >= MAX_RETRIES
    delay = next_attempt_delay(max(0, attempt_count - 1))
    next_at = None if terminal else datetime.now(timezone.utc) + timedelta(seconds=delay)
    await conn.execute(
        """
        UPDATE events
           SET last_error = $2,
               next_attempt_at = $3,
               claimed_at = NULL,
               abandoned_at = CASE WHEN $4 THEN NOW() ELSE NULL END
         WHERE id = $1
        """,
        event_id, (error or "")[:500], next_at, terminal,
    )


async def _release_unattempted(conn, rows: list[dict]) -> None:
    if not rows:
        return
    await conn.execute(
        """
        UPDATE events
           SET attempts = GREATEST(attempts - 1, 0),
               next_attempt_at = NOW(), claimed_at = NULL
         WHERE id = ANY($1::bigint[])
           AND redis_published_at IS NULL
           AND abandoned_at IS NULL
           AND claimed_at = $2
        """,
        [row["id"] for row in rows],
        rows[0]["claimed_at"],
    )


def _xadd_fields(row: dict) -> dict[bytes | str, bytes | str]:
    """Pack a row into Redis stream fields. All values are encoded as
    bytes — subscribers parse `payload` as JSON, the rest as UTF-8.

    The return type widens to the union redis-py's `xadd` stub
    declares (`bytes | bytearray | memoryview | str | int | float`);
    we only ever fill it with bytes, but dict invariance means
    `dict[bytes, bytes]` is not a subtype of the wider union the
    stub expects."""
    # asyncpg returns JSONB as either a dict/list or a str depending on
    # codec registration; normalise to a JSON string for the stream.
    payload = row["payload"]
    if isinstance(payload, (dict, list)):
        payload_str = json.dumps(payload, separators=(",", ":"))
    else:
        payload_str = payload or "{}"

    fields: dict[bytes | str, bytes | str] = {
        b"id": str(row["id"]).encode(),
        b"occurred_at": row["occurred_at"].isoformat().encode(),
        b"kind": row["kind"].encode(),
        b"payload": payload_str.encode(),
    }
    if row.get("vault_id") is not None:
        fields[b"vault_id"] = str(row["vault_id"]).encode()
    if row.get("resource_uri"):
        # Canonical handle — same shape MCP clients see. Stream
        # consumers parse it with the standard akb:// regex to recover
        # vault + type + identifier.
        fields[b"resource_uri"] = row["resource_uri"].encode()
    if row.get("actor_id") is not None:
        fields[b"actor_id"] = str(row["actor_id"]).encode()
    return fields


async def _process_once() -> int:
    if not settings.redis_url:
        # Defensive — start_workers shouldn't have started us, but if
        # config gets reloaded to empty mid-flight we still no-op.
        return 0

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch = await _claim_batch(conn)
        if not batch:
            return 0

        try:
            client = await _client()
        except Exception as e:  # noqa: BLE001
            # The connection attempt represents work on one row, not all rows
            # claimed with it. Preserve the first row's failure/backoff and
            # return claim credit for the untouched remainder.
            first, *unattempted = batch
            await _mark_failure(
                conn, first["id"], first["attempts"], f"redis client init: {e}",
            )
            await _release_unattempted(conn, unattempted)
            return 0

        succeeded = 0
        for position, row in enumerate(batch):
            # Gardener bookkeeping is marked published WITHOUT an XADD: it
            # stays queryable in PG (the event tail still serves it) but never
            # enters the stream, so the daemon stops waking on its own writes.
            # The skip is counted as success — the row is drained, not retried.
            # Identity is (gdn-* vault, exact table name) via the registry, so
            # a user vault's same-named table still publishes. Unresolvable
            # rows fail open (publish).
            if row.get("kind") == "table.rows_changed" and await _is_suppressed(conn, row):
                await _mark_published(conn, row["id"])
                succeeded += 1
                continue
            fields = _xadd_fields(row)
            try:
                # redis-py's xadd stub takes the wider
                # `dict[bytes|bytearray|memoryview|str|int|float, ...]`
                # union; dict invariance means our narrower
                # `dict[bytes|str, bytes|str]` is rejected even though
                # every value we put is one of the accepted types.
                # Runtime conversion is unchanged.
                await client.xadd(
                    settings.redis_event_stream,
                    cast(dict, fields),
                    maxlen=settings.redis_stream_maxlen,
                    approximate=True,
                )
            except Exception as e:  # noqa: BLE001
                # Whole batch likely doomed if Redis is down — mark this
                # one and short-circuit so we don't hammer a downed
                # broker. Loop's idle backoff then kicks in.
                await _mark_failure(conn, row["id"], row["attempts"], str(e))
                await _release_unattempted(conn, batch[position + 1:])
                logger.warning("XADD failed for event %s: %s", row["id"], e)
                # Drop the cached client so the next tick reconnects;
                # otherwise a dead connection sticks around.
                await close_client()
                return succeeded

            await _mark_published(conn, row["id"])
            succeeded += 1

        return succeeded


# ── Sweeper ──────────────────────────────────────────────────────


async def _sweep_once() -> int:
    """Purge published rows older than SWEEP_GRACE_INTERVAL.
    Rate-limited to once per SWEEP_INTERVAL_SECONDS."""
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at < SWEEP_INTERVAL_SECONDS:
        return 0
    _last_sweep_at = now
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            f"""
            WITH d AS (
                DELETE FROM events
                 WHERE redis_published_at IS NOT NULL
                   AND redis_published_at < NOW() - INTERVAL '{SWEEP_GRACE_INTERVAL}'
                RETURNING 1
            )
            SELECT COUNT(*) FROM d
            """
        )
    n = int(n or 0)
    if n:
        logger.info("events sweep: purged %d rows", n)
    return n


# ── Loop ─────────────────────────────────────────────────────────


async def _tick() -> int:
    try:
        n = await _process_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("events_publisher tick failed: %s", e)
        n = 0
    try:
        await _sweep_once()
    except Exception as e:  # noqa: BLE001
        logger.exception("events_publisher sweep failed: %s", e)
    return n


_runner = BackfillRunner("events_publisher", _tick)
start = _runner.start


async def stop() -> None:
    await _runner.stop()
    await close_client()


# ── Stats (for /health) ──────────────────────────────────────────


async def pending_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE redis_published_at IS NULL AND abandoned_at IS NULL) AS pending,
                COUNT(*) FILTER (WHERE redis_published_at IS NULL
                                 AND abandoned_at IS NULL AND attempts > 0)                AS retrying,
                COUNT(*) FILTER (WHERE redis_published_at IS NULL
                                 AND abandoned_at IS NOT NULL)                             AS abandoned,
                COUNT(*) FILTER (WHERE redis_published_at IS NOT NULL)                    AS published
              FROM events
            """
        )
    return {
        "pending":   int(row["pending"]),
        "retrying":  int(row["retrying"]),
        "abandoned": int(row["abandoned"]),
        "published": int(row["published"]),
        "stream":    settings.redis_event_stream,
    }
