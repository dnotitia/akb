"""Installation-wide publication password-attempt budgets.

PostgreSQL is already required to resolve a publication. One row per protected
publication serializes reservations across API processes, before bcrypt, without
holding its accounting connection or row lock during verification. No Redis or
process-local fallback is used.

The existing accounting is unchanged: five free per-IP / thirty per-publication
attempts; the crossing attempt still verifies, and followers back off. Correct
passwords refund their reservation and unlock the counter. A quiet hour decays
counts. Manager and HMAC-token bypasses remain at the route layer.

Only live protected publications allocate state. Rows cascade with publication
deletion, and each row holds at most 128 pseudonymized IP entries. Expired IP
entries are pruned on use; capacity never evicts a live lock. A DB reservation
failure is unavailable, not permission to try a password. A failed refund is
conservative and must not turn an already-verified view into a failed request.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging

import asyncpg

from app.db.postgres import get_pool

logger = logging.getLogger(__name__)

_FREE_IP = 5
_FREE_SLUG = 30
_BACKOFF = (10.0, 30.0, 120.0, 600.0, 1800.0, 3600.0)
_DECAY_SECS = 3600.0
_MAX_IP_ENTRIES = 128


class BudgetUnavailable(Exception):
    """The shared password budget could not safely reserve an attempt."""


@dataclass
class _E:
    failures: int = 0
    locked_until: float = 0.0
    last: float = 0.0


@dataclass
class _Budget:
    total: _E = field(default_factory=_E)
    ips: dict[str, _E] = field(default_factory=dict)

    @classmethod
    def load(cls, raw: str | dict) -> _Budget:
        state = json.loads(raw) if isinstance(raw, str) else raw
        return cls(
            total=_E(**state.get("total", {})),
            ips={key: _E(**entry) for key, entry in state.get("ips", {}).items()},
        )


def _wait(entry: _E | None, now: float) -> float:
    return max(0.0, entry.locked_until - now) if entry is not None else 0.0


def _bump(entry: _E, free: int, now: float) -> None:
    if now - entry.last > _DECAY_SECS:
        entry.failures = 0
        entry.locked_until = 0.0
    entry.failures += 1
    entry.last = now
    if entry.failures > free:
        step = min(entry.failures - free - 1, len(_BACKOFF) - 1)
        entry.locked_until = now + _BACKOFF[step]


def _reserve(budget: _Budget, ip_key: str, now: float) -> float:
    budget.ips = {
        key: entry for key, entry in budget.ips.items()
        if now - entry.last <= _DECAY_SECS or entry.locked_until > now
    }
    wait = max(_wait(budget.total, now), _wait(budget.ips.get(ip_key), now))
    if wait:
        return wait
    if ip_key not in budget.ips and len(budget.ips) >= _MAX_IP_ENTRIES:
        unlocked = [key for key, entry in budget.ips.items() if entry.locked_until <= now]
        if not unlocked:
            return min(_wait(entry, now) for entry in budget.ips.values())
        del budget.ips[min(unlocked, key=lambda key: budget.ips[key].last)]
    _bump(budget.ips.setdefault(ip_key, _E()), _FREE_IP, now)
    _bump(budget.total, _FREE_SLUG, now)
    return 0.0


def _release(budget: _Budget, ip_key: str) -> None:
    budget.ips.pop(ip_key, None)
    budget.total.failures = max(0, budget.total.failures - 1)
    budget.total.locked_until = 0.0


async def _change(slug: str, ip: str, *, refund: bool) -> float:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Keep contention on a public request path bounded. This does not
            # increase the existing API/DB timeout or permit a local fallback.
            await conn.execute("SET LOCAL lock_timeout = '1s'")
            await conn.execute("SET LOCAL statement_timeout = '2s'")
            publication_id = await conn.fetchval(
                """
                SELECT id FROM publications
                 WHERE slug=$1 AND password_hash IS NOT NULL
                   AND (expires_at IS NULL OR expires_at > clock_timestamp())
                 FOR KEY SHARE
                """,
                slug,
            )
            if publication_id is None:
                return 0.0
            if not refund:
                await conn.execute(
                    "INSERT INTO publication_password_budgets(publication_id) VALUES($1) "
                    "ON CONFLICT (publication_id) DO NOTHING",
                    publication_id,
                )
            raw = await conn.fetchval(
                "SELECT state FROM publication_password_budgets WHERE publication_id=$1 FOR UPDATE",
                publication_id,
            )
            if raw is None:
                return 0.0
            # Read the DB clock AFTER acquiring the lock: workers can have
            # different clocks, and time spent waiting must not extend a lock.
            now = await conn.fetchval("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision")
            budget = _Budget.load(raw)
            before = asdict(budget)
            # Per-publication hashing avoids retaining raw forwarded IPs or
            # correlating this transient state between different publications.
            ip_key = hashlib.sha256(publication_id.bytes + ip.encode()).hexdigest()
            if refund:
                _release(budget, ip_key)
                wait = 0.0
            else:
                wait = _reserve(budget, ip_key, now)
            after = asdict(budget)
            if after != before:
                await conn.execute(
                    "UPDATE publication_password_budgets SET state=$2::jsonb WHERE publication_id=$1",
                    publication_id, json.dumps(after, separators=(",", ":")),
                )
            return wait


async def reserve(slug: str, ip: str) -> float:
    """Reserve before password verification; never fail open on DB errors."""
    try:
        return await _change(slug, ip, refund=False)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        logger.warning("Publication password budget unavailable", exc_info=True)
        raise BudgetUnavailable from exc


async def release(slug: str, ip: str) -> None:
    """Refund a verified password without failing an already-authorized view."""
    try:
        await _change(slug, ip, refund=True)
    except (OSError, TimeoutError, asyncpg.PostgresError):
        logger.warning("Publication password budget refund failed; reservation retained", exc_info=True)
