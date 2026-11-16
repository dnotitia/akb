"""AI connection registry — which AI apps use an account over MCP.

**Why it did not exist.** An OAuth MCP connection authenticates with a
Keycloak access token, so AKB holds no row for it at all; a PAT connection
touches only ``tokens.last_used_at``, which says a key was used but not by
which app, and is also bumped by plain REST callers. Neither can tell a person
"Claude Code connected to your account yesterday", tell support which client
version a user runs, or tell onboarding that the first connection happened.

**Shape.** One row per (account, credential): the OAuth token's authorized
party (``azp``, one Keycloak client registration per MCP client under dynamic
client registration) or the PAT's token id. The row carries the client's
self-reported ``clientInfo`` name and version, the User-Agent as a fallback,
and first/last seen times. No IP address and no request content are stored.

**Where clientInfo comes from.** The MCP transport is stateless, so the SDK
session holds no ``initialize`` parameters on a later ``tools/call``. The
client describes itself only in the body of the request that carries it: the
legacy ``initialize`` request, or the 2026-07-28 per-request
``_meta["io.modelcontextprotocol/clientInfo"]``. The HTTP adapter already
decodes that body, so it reports the observation from there.

**Cost.** ``observe()`` runs on the single event loop that serves every
request, so it only touches memory: a throttle lookup, and on a miss one dict
write. A background runner folds pending observations into PostgreSQL. The
throttle keeps ``last_seen_at`` to at most one write per connection per
minute, in process and again in the UPSERT's ``WHERE`` so several replicas
agree.

**New connections.** The first insert for a connection enqueues an
``account.ai_connection`` notification in the same transaction, so an
account holder learns that a new AI app is using the account.

**Never raises.** Recording a connection must not be able to fail or delay
the request that revealed it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple

import asyncpg
from mcp_types import CLIENT_INFO_META_KEY

from app.db.postgres import get_pool
from app.repositories.notifications_repo import enqueue_notification_event
from app.services._backfill import BackfillRunner

logger = logging.getLogger("akb.ai_connections")

NOTIFICATION_KIND = "account.ai_connection"

# `last_seen_at` is written at most once per connection per window. A minute is
# fine-grained enough for "last used" and for onboarding to notice a first
# connection, and coarse enough that an agent calling tools in a tight loop
# costs one small UPSERT a minute rather than one per call.
SEEN_WINDOW_SECS = 60

# Short, because onboarding waits on the first row: a person who just connected
# their AI app should see it within seconds, not a minute later.
FLUSH_INTERVAL_SECS = 5

# Distinct connections waiting for the next flush. A connection already
# pending is updated in place, so this only bounds a flood of NEW keys between
# two flushes. Overflow is counted, never silent.
MAX_PENDING = 10_000

# In-process throttle entries. Older entries are pruned when the map reaches
# the bound; a pruned key costs at most one extra UPSERT, which the database
# throttle then absorbs.
MAX_THROTTLE = 50_000

SHUTDOWN_BUDGET_SECS = 6.0

NAME_MAX = 128
VERSION_MAX = 64
USER_AGENT_MAX = 256
CLIENT_KEY_MAX = 255

AUTH_KINDS = ("oauth", "pat")

# One statement per observation. The conflict arm writes only when the window
# has passed, the client reported a different name/version, or a User-Agent is
# known for the first time, which is what keeps `last_seen_at` to one write a
# minute across replicas. `xmax = 0` is true only for a freshly inserted row:
# an ON CONFLICT update stamps the updating transaction id there.
_UPSERT_SQL = """
    INSERT INTO ai_connections AS c
        (user_id, auth_kind, client_key, token_id, client_name, client_version,
         user_agent, first_seen_at, last_seen_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
    ON CONFLICT (user_id, auth_kind, client_key) DO UPDATE
       SET last_seen_at   = GREATEST(c.last_seen_at, EXCLUDED.last_seen_at),
           client_name    = COALESCE(EXCLUDED.client_name, c.client_name),
           client_version = CASE WHEN EXCLUDED.client_name IS NOT NULL
                                 THEN EXCLUDED.client_version
                                 ELSE c.client_version END,
           user_agent     = COALESCE(EXCLUDED.user_agent, c.user_agent)
     WHERE c.last_seen_at <= EXCLUDED.last_seen_at - make_interval(secs => $9)
        OR (EXCLUDED.client_name IS NOT NULL
            AND (c.client_name IS DISTINCT FROM EXCLUDED.client_name
                 OR c.client_version IS DISTINCT FROM EXCLUDED.client_version))
        OR (c.user_agent IS NULL AND EXCLUDED.user_agent IS NOT NULL)
    RETURNING id, (xmax = 0) AS inserted
"""


class _Key(NamedTuple):
    user_id: str
    auth_kind: str
    client_key: str


@dataclass
class _Observation:
    """Latest pending facts for one connection. Folded, never appended."""

    token_id: str | None
    client_name: str | None
    client_version: str | None
    user_agent: str | None
    seen_at: datetime

    def merge(self, newer: "_Observation") -> None:
        self.seen_at = max(self.seen_at, newer.seen_at)
        if newer.client_name is not None:
            self.client_name = newer.client_name
            self.client_version = newer.client_version
        if newer.user_agent is not None:
            self.user_agent = newer.user_agent


# key -> (monotonic time last queued, last reported name, last reported version)
_throttle: dict[_Key, tuple[float, str | None, str | None]] = {}
_pending: dict[_Key, _Observation] = {}
_dropped = 0
_flush_failures = 0


# ── Extraction ──────────────────────────────────────────────────


def _clip(value: Any, limit: int) -> str | None:
    """A bounded, printable, single-line string, or None.

    Every one of these values is self-reported by the client and later shown
    to a person, so control and format characters are removed rather than
    rendered, and the length is bounded before it reaches a column.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(
        ch for ch in value if unicodedata.category(ch)[0] != "C"
    ).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def client_info_from_body(body: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """The (name, version) a client reported about itself in this request.

    ``initialize`` carries ``params.clientInfo``; the 2026-07-28 envelope
    carries the same object under ``params._meta``. Anything else reports
    nothing, and the caller falls back to the User-Agent.
    """
    if not isinstance(body, dict):
        return None, None
    params = body.get("params")
    if not isinstance(params, dict):
        return None, None
    info: Any = params.get("clientInfo") if body.get("method") == "initialize" else None
    if not isinstance(info, dict):
        meta = params.get("_meta")
        info = meta.get(CLIENT_INFO_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return None, None
    name = _clip(info.get("name"), NAME_MAX)
    if name is None:
        return None, None
    return name, _clip(info.get("version"), VERSION_MAX)


def user_agent_product(user_agent: str | None) -> tuple[str | None, str | None]:
    """The leading ``product/version`` token of a User-Agent header.

    Used only for display when a client never reported ``clientInfo``.
    """
    if not user_agent:
        return None, None
    product = user_agent.split()[0] if user_agent.split() else ""
    name, _, version = product.partition("/")
    return _clip(name, NAME_MAX), _clip(version, VERSION_MAX)


def connection_key(user: Any) -> tuple[_Key, str | None] | None:
    """Which connection a resolved principal belongs to, if it is one.

    Only human accounts are recorded: the registry answers "which AI apps use
    my account". Service and publishable keys are infrastructure, not apps a
    person connected.
    """
    if getattr(user, "account_kind", None) != "human":
        return None
    user_id = getattr(user, "user_id", None)
    if not isinstance(user_id, str) or not user_id:
        return None
    method = getattr(user, "auth_method", None)
    if method == "pat":
        token_id = getattr(user, "token_id", None)
        if getattr(user, "key_class", None) != "pat" or not isinstance(token_id, str):
            return None
        return _Key(user_id, "pat", token_id), token_id
    if method == "oauth":
        client_id = _clip(getattr(user, "oauth_client_id", None), CLIENT_KEY_MAX)
        if client_id is None:
            return None
        return _Key(user_id, "oauth", client_id), None
    return None


# ── Recording ───────────────────────────────────────────────────


def observe(
    user: Any,
    *,
    body: dict[str, Any] | None = None,
    user_agent: str | None = None,
) -> None:
    """Note that an authenticated MCP request arrived. Memory only; never raises."""
    global _dropped
    try:
        resolved = connection_key(user)
        if resolved is None:
            return
        key, token_id = resolved
        name, version = client_info_from_body(body)
        now = time.monotonic()
        previous = _throttle.get(key)
        if previous is not None:
            queued_at, last_name, last_version = previous
            unchanged = name is None or (name, version) == (last_name, last_version)
            if unchanged and now - queued_at < SEEN_WINDOW_SECS:
                return
        observation = _Observation(
            token_id=token_id,
            client_name=name,
            client_version=version,
            user_agent=_clip(user_agent, USER_AGENT_MAX),
            seen_at=datetime.now(timezone.utc),
        )
        pending = _pending.get(key)
        if pending is not None:
            pending.merge(observation)
        elif len(_pending) >= MAX_PENDING:
            _dropped += 1
            if _dropped == 1 or _dropped % 1000 == 0:
                logger.warning(
                    "ai_connections pending map full (%d); dropped %d observation(s)",
                    MAX_PENDING, _dropped,
                )
            return
        else:
            _pending[key] = observation
        if len(_throttle) >= MAX_THROTTLE and key not in _throttle:
            _prune_throttle(now)
        if name is None and previous is not None:
            name, version = previous[1], previous[2]
        _throttle[key] = (now, name, version)
    except Exception as e:  # noqa: BLE001 — recording must never fail a request
        _dropped += 1
        if _dropped == 1 or _dropped % 1000 == 0:
            logger.warning("ai_connections.observe failed (%d lost so far): %s", _dropped, e)


def _prune_throttle(now: float) -> None:
    stale = [k for k, (at, _, _) in _throttle.items() if now - at >= SEEN_WINDOW_SECS]
    for k in stale:
        del _throttle[k]
    if len(_throttle) >= MAX_THROTTLE:
        # Every entry is fresh: a burst of distinct connections inside one
        # window. Forgetting them costs extra UPSERTs the database throttle
        # absorbs, never a wrong row.
        _throttle.clear()


def pending_depth() -> int:
    return len(_pending)


def dropped_count() -> int:
    return _dropped


def stats() -> dict:
    """Operational snapshot for `/health`."""
    return {
        "pending": len(_pending),
        "lost": _dropped,
        "consecutive_flush_failures": _flush_failures,
    }


# ── Flushing ────────────────────────────────────────────────────


async def _write_one(conn, key: _Key, obs: _Observation) -> bool:
    """UPSERT one connection; enqueue the notice when it is new. True if inserted."""
    async with conn.transaction():
        row = await conn.fetchrow(
            _UPSERT_SQL,
            uuid.UUID(key.user_id),
            key.auth_kind,
            key.client_key,
            uuid.UUID(obs.token_id) if obs.token_id else None,
            obs.client_name,
            obs.client_version,
            obs.user_agent,
            obs.seen_at,
            float(SEEN_WINDOW_SECS),
        )
        if row is None or not row["inserted"]:
            return False
        await enqueue_notification_event(
            conn,
            NOTIFICATION_KIND,
            source_key=f"ai_connection:{row['id']}",
            resource_id=row["id"],
            recipient_ids=[key.user_id],
        )
        return True


async def flush_once() -> int:
    """Fold every pending observation into PostgreSQL. `BackfillRunner` callback.

    The pending map is taken whole, so an observation recorded while the write
    is in flight lands in the next batch. A row whose account or token vanished
    in between (a revoked PAT) is dropped: there is no connection left to
    describe. Any other failure merges the unwritten remainder back and is
    retried on the next tick: a database restart delays rows, never loses them.
    """
    global _flush_failures, _dropped
    if not _pending:
        return 0
    claimed = list(_pending.items())
    _pending.clear()
    written = 0
    index = 0
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            for index, (key, obs) in enumerate(claimed):
                try:
                    await _write_one(conn, key, obs)
                except (asyncpg.ForeignKeyViolationError, asyncpg.CheckViolationError, ValueError):
                    _dropped += 1
                    continue
                written += 1
            index = len(claimed)
    except asyncio.CancelledError:
        # A shutdown deadline firing mid-write must not leave the claimed
        # observations nowhere: popped from the map and never written.
        _requeue(claimed[index:])
        raise
    except Exception as e:  # noqa: BLE001 — an environment fault, not a property of the rows
        _requeue(claimed[index:])
        _flush_failures += 1
        if _flush_failures == 1 or _flush_failures % 60 == 0:
            logger.warning(
                "ai_connections flush failing (%d consecutive; %d requeued): %s",
                _flush_failures, len(claimed) - index, e,
            )
        return written
    _flush_failures = 0
    return written


def _requeue(batch: list[tuple[_Key, _Observation]]) -> None:
    """Return unwritten observations; one recorded meanwhile is newer and wins."""
    for key, obs in batch:
        newer = _pending.get(key)
        if newer is not None:
            obs.merge(newer)
        _pending[key] = obs


_flusher = BackfillRunner(
    "ai_connections_flusher", flush_once,
    idle_secs=FLUSH_INTERVAL_SECS,
    log_progress=False,
)


def reset() -> None:
    """Drop every pending observation and throttle entry. Startup and tests only."""
    global _dropped, _flush_failures
    _pending.clear()
    _throttle.clear()
    _dropped = 0
    _flush_failures = 0


def start() -> None:
    _flusher.start()


async def stop() -> None:
    """Quiesce the flusher, then drain what is left. Never raises."""
    budget = SHUTDOWN_BUDGET_SECS / 2
    try:
        await _flusher.stop(timeout=budget)
    except Exception:  # noqa: BLE001
        logger.warning("ai_connections flusher stop failed", exc_info=True)
    try:
        if _pending:
            await asyncio.wait_for(flush_once(), timeout=budget)
    except Exception:  # noqa: BLE001
        logger.warning(
            "ai_connections final flush failed; %d observation(s) not recorded",
            pending_depth(), exc_info=True,
        )


# ── Reading ─────────────────────────────────────────────────────


async def list_connections(user_id: str) -> list[dict]:
    """This account's connections, most recently seen first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.auth_kind, c.token_id, t.token_prefix, t.name AS token_name,
                   c.client_name, c.client_version, c.user_agent,
                   c.first_seen_at, c.last_seen_at
              FROM ai_connections c
              LEFT JOIN tokens t ON t.id = c.token_id
             WHERE c.user_id = $1
             ORDER BY c.last_seen_at DESC, c.id
            """,
            uuid.UUID(user_id),
        )
    return [_present(row) for row in rows]


def _present(row) -> dict:
    name, version = row["client_name"], row["client_version"]
    if name is None:
        name, version = user_agent_product(row["user_agent"])
    is_pat = row["auth_kind"] == "pat"
    return {
        "id": str(row["id"]),
        "client_name": name,
        "client_version": version,
        "auth_kind": row["auth_kind"],
        "token_id": str(row["token_id"]) if is_pat and row["token_id"] else None,
        "token_prefix": row["token_prefix"] if is_pat else None,
        "token_name": row["token_name"] if is_pat else None,
        "first_seen_at": row["first_seen_at"].isoformat(),
        "last_seen_at": row["last_seen_at"].isoformat(),
    }

