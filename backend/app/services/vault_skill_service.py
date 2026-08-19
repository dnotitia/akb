"""Vault-skill auto-injection state: version cache + per-session tracking.

`mcp_server/server.py` calls `injection_payload()` from the dispatch
chokepoint. Contract mirrors `tool_usage.record`: NEVER raises, never blocks
the tool call on failure — a lookup error is logged and skipped.

THIS MODULE PERFORMS NO AUTHORIZATION, and must not be called as though it
did. The chokepoint only reaches it when the vault name matches
`access_service.authorized_vault()` — i.e. a `check_vault_access` that
SUCCEEDED during the same call. Two consequences that the code below relies
on: cache keys are real, already-authorized vault names rather than arbitrary
caller strings, and a caller cannot use cache-population side effects as a
vault-existence oracle. The bounds here (LRU + name clamp) deliberately do NOT
depend on that coupling — they hold even if a future caller forgets it.

Session tracking is (session_id, vault) → last-injected skill version, so a
long-lived session re-receives the skill exactly when it changes. The vault
cache holds (version, body) with a short TTL plus write-through invalidation.

Every in-process writer that can change what this cache holds calls
`invalidate()` AFTER its transaction commits: document update AND legacy
`edit()` in both backends, vault delete, and vault CREATE (the cache is
name-keyed, so a same-named recreate must not inherit the old vault's entry,
and a name probed before it existed must not keep its negative entry).
Invalidating pre-commit would be worse than not invalidating: a concurrent
reader would miss, re-read the pre-commit row, re-cache it, and no second
invalidation would ever follow. With that discipline the TTL is genuinely just
the cross-process safety net — it is what would bound staleness if the API
ever ran more than one replica (it is replicas=1 today), not a cover for
same-process gaps.

A cache MISS costs several DB round trips plus a git read, so it is
single-flighted per vault (concurrent first-touches share one fetch) and
bounded by `fetch_timeout_secs`. A timed-out fetch caches NOTHING — turning a
transient stall into a TTL-long negative entry would silence injection for
every session long after the stall passed. A CANCELLED leader likewise hands
its followers a miss rather than its own CancelledError: cancellation belongs
to the cancelled caller, and a BaseException would slip past the never-raises
guard below into a tool call that has already done its work.

Mirror vaults are excluded even when the upstream repo carries an
overview/vault-skill.md: auto-injecting upstream-controlled markdown into
every agent session would be an instruction-injection vector. akb_help still
serves such a file on explicit request; only the automatic channel is closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict

from app.config import settings
from app.exceptions import NotFoundError
from app.services import skill_policy

logger = logging.getLogger("akb.vault_skill")

_BODY_MAX = settings.vault_skill.body_max_bytes
_CACHE_TTL = settings.vault_skill.cache_ttl_secs
_SESSION_MAP_MAX = settings.vault_skill.session_map_max
_VAULT_CACHE_MAX = settings.vault_skill.vault_cache_max
_FETCH_TIMEOUT = settings.vault_skill.fetch_timeout_secs

# Refuse outright rather than key a cache entry on it. `validate_vault_name`
# caps real names far below this, so the clamp only ever rejects input that
# could not name a vault anyway — it exists so the cache bound cannot be
# defeated by making each key enormous instead of making many keys.
_VAULT_NAME_MAX = 256

# One timeout log per this many seconds. A stalled DB makes EVERY call time
# out; the interesting signal is "it is happening", not one line per request.
_TIMEOUT_LOG_EVERY = 60.0
_last_timeout_log = 0.0

# vault → (fetched_at_monotonic, version | None, body | None). A (None, None)
# entry is a NEGATIVE cache: absent doc or mirror vault, so the repeated
# lookups a mirror-heavy tenant would otherwise pay are amortized too. LRU by
# insertion/refresh order: the TTL governs FRESHNESS and evicts nothing, so
# without a bound this map would grow with the number of distinct vault names
# ever touched and never shrink.
_vault_cache: OrderedDict[str, tuple[float, str | None, str | None]] = OrderedDict()
# (session_id, vault) → injected version. OrderedDict as LRU.
_session_map: OrderedDict[tuple[str, str], str] = OrderedDict()
# vault → in-flight fetch. Single-flight: a cache miss costs several DB round
# trips plus a git read, so N concurrent first-touches of one vault must not
# become N stampeding fetches. Entries are removed in the leader's `finally`,
# so the map holds at most one entry per vault being fetched right now.
_pending: dict[str, asyncio.Future] = {}


def reset() -> None:
    """Clear all state and re-read the configured bounds.

    Called by tests, and available to startup. Bounds are re-read here rather
    than per call so the hot path stays plain global reads — the same trade
    `tool_usage.reset()` makes.
    """
    global _BODY_MAX, _CACHE_TTL, _SESSION_MAP_MAX, _VAULT_CACHE_MAX
    global _FETCH_TIMEOUT, _last_timeout_log
    _BODY_MAX = settings.vault_skill.body_max_bytes
    _CACHE_TTL = settings.vault_skill.cache_ttl_secs
    _SESSION_MAP_MAX = settings.vault_skill.session_map_max
    _VAULT_CACHE_MAX = settings.vault_skill.vault_cache_max
    _FETCH_TIMEOUT = settings.vault_skill.fetch_timeout_secs
    _last_timeout_log = 0.0
    _vault_cache.clear()
    _session_map.clear()
    _pending.clear()


def invalidate(vault: str) -> None:
    """Write-through hook: a commit landed on the canonical path, or the vault
    itself was deleted. Callers MUST be past their commit — see module docs."""
    _vault_cache.pop(vault, None)


async def _fetch_skill(vault: str) -> dict | None:
    """Fetch the canonical doc. None = absent OR external-git mirror vault.

    Raises on real errors — the caller separates absence from failure.
    Local imports avoid a service-import cycle at module load.

    The mirror check runs FIRST and short-circuits: two indexed single-column
    reads (vaults.name unique index, then the vault_external_git PK) are
    cheaper than a document get, which additionally reads the body out of git.
    Both go through the owning repositories rather than ad-hoc SQL, per
    `vault_external_git_repo`'s module contract.
    """
    from app.db.postgres import get_pool
    from app.repositories.vault_external_git_repo import VaultExternalGitRepository
    from app.repositories.vault_repo import VaultRepository
    from app.services.revision_backend import get_document_service

    pool = await get_pool()
    vault_id = await VaultRepository(pool).get_id_by_name(vault)
    if vault_id is None:
        return None
    if await VaultExternalGitRepository(pool).exists(vault_id):
        return None

    try:
        doc = await get_document_service().get(vault, skill_policy.VAULT_SKILL_PATH)
    except NotFoundError:
        return None

    content = doc.content or ""
    version = (doc.content_hash or doc.current_commit or "")[:16]
    if not version:
        # Both identity columns null (pre-hash legacy row that never got a
        # commit recorded). Derive the version from the body instead of
        # skipping: "re-inject when the text changes" is exactly the semantic,
        # and a silent never-inject would be indistinguishable from absence.
        version = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return {"content": content, "version": version}


def _store(vault: str, version: str | None, body: str | None) -> None:
    """Write a cache entry and hold the LRU bound."""
    _vault_cache[vault] = (time.monotonic(), version, body)
    _vault_cache.move_to_end(vault)
    while len(_vault_cache) > _VAULT_CACHE_MAX:
        _vault_cache.popitem(last=False)


def _log_timeout(vault: str) -> None:
    global _last_timeout_log
    now = time.monotonic()
    if now - _last_timeout_log >= _TIMEOUT_LOG_EVERY:
        _last_timeout_log = now
        logger.warning(
            "vault_skill fetch exceeded %.1fs (most recently for %s); "
            "injection skipped", _FETCH_TIMEOUT, vault,
        )


async def _current(vault: str) -> tuple[str | None, str | None]:
    hit = _vault_cache.get(vault)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        _vault_cache.move_to_end(vault)
        return hit[1], hit[2]

    inflight = _pending.get(vault)
    if inflight is not None:
        # `shield` so a cancelled FOLLOWER cannot cancel the shared fetch out
        # from under the other waiters (awaiting a Future directly would).
        return await asyncio.shield(inflight)

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[vault] = fut
    try:
        try:
            doc = await asyncio.wait_for(_fetch_skill(vault), timeout=_FETCH_TIMEOUT)
        except TimeoutError:
            # Deliberately NO cache write: a transient stall must not turn
            # into a TTL-long negative entry that suppresses injection for
            # every session long after the DB recovered.
            _log_timeout(vault)
            result: tuple[str | None, str | None] = (None, None)
            fut.set_result(result)
            return result
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                # The leader's cancellation belongs to the LEADER's caller
                # alone. Handing it to followers via the shared future would
                # raise CancelledError inside their `shield` await — a
                # BaseException, so it escapes both `except Exception` guards
                # and aborts an unrelated tool call that already committed its
                # business work. Followers observe a miss instead; nothing is
                # cached, so the next call fetches fresh.
                fut.set_result((None, None))
            else:
                fut.set_exception(e)
                fut.exception()  # mark retrieved; followers re-raise it too
            raise  # the leader itself must still cancel/fail
        if doc is None:
            result = (None, None)
        else:
            result = (doc["version"], doc["content"])
        _store(vault, result[0], result[1])
        fut.set_result(result)
        return result
    finally:
        _pending.pop(vault, None)


async def injection_payload(session_id: str | None, vault: str) -> dict | None:
    """The `vault_skill` payload to attach, or None. Never raises."""
    try:
        if not settings.vault_skill.enabled:
            return None
        # Before any key is formed, in either map: an over-long name is
        # refused outright and caches nothing.
        if len(vault) > _VAULT_NAME_MAX:
            return None
        # Stdio/CLI callers have no session id; treat them as one
        # per-process pseudo-session (a stdio proxy is one conversation).
        key = (session_id or "no-session", vault)
        version, body = await _current(vault)
        if version is None or body is None:
            return None
        prev = _session_map.get(key)
        if prev == version:
            _session_map.move_to_end(key)
            return None
        _session_map[key] = version
        _session_map.move_to_end(key)
        while len(_session_map) > _SESSION_MAP_MAX:
            _session_map.popitem(last=False)
        truncated = len(body.encode("utf-8")) > _BODY_MAX
        if truncated:
            clipped = body.encode("utf-8")[:_BODY_MAX].decode("utf-8", "ignore")
            body = (
                clipped
                + "\n\n[truncated — call akb_help(topic=\"vault-skill\", vault=\""
                + vault + "\") for the full text]"
            )
        return {
            "vault": vault,
            "version": version,
            "reason": "updated" if prev is not None else "first_touch",
            "body": body,
            "truncated": truncated,
        }
    except Exception as e:  # noqa: BLE001 — injection must never fail a tool call
        logger.warning("vault_skill injection skipped for %s: %s", vault, e)
        return None
