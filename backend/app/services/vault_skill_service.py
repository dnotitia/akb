"""Vault-skill auto-injection state: version cache + per-session tracking.

`mcp_server/server.py` calls `injection_payload()` from the dispatch
chokepoint. Contract mirrors `tool_usage.record`: NEVER raises, never blocks
the tool call on failure — a lookup error is logged and skipped.

THIS MODULE PERFORMS NO AUTHORIZATION. Callers pass the immutable vault id
returned by the completed reader access check. The service re-verifies that
the name still resolves to that id before reading, which closes the
delete/recreate gap without duplicating ACL policy here.

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
every agent session would be an instruction-injection vector. Explicit help
uses the same exclusion and reports that mirrors do not have an owner guide.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
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
_CacheKey = tuple[str, str | None]
_vault_cache: OrderedDict[_CacheKey, tuple[float, str | None, str | None]] = OrderedDict()
# (session_id, vault, immutable vault id) → additively injected version.
# OrderedDict as LRU.  This is intentionally separate from strict write
# acknowledgement: putting a guide in a read response does not prove that the
# client applied it before a concurrently submitted write.
_session_map: OrderedDict[tuple[str, str, str | None], str] = OrderedDict()
# Strict preflight state.  Acknowledgement is explicit in capability v2, so all
# concurrent writes that arrived without the challenge token remain
# non-mutating.  Challenges are opaque and bound by their map key to the MCP
# session, vault name, and immutable vault id.
_acknowledged_map: OrderedDict[tuple[str, str, str | None], str] = OrderedDict()
_challenge_map: OrderedDict[
    tuple[str, str, str | None], tuple[str, str]
] = OrderedDict()
# vault → in-flight fetch. Single-flight: a cache miss costs several DB round
# trips plus a git read, so N concurrent first-touches of one vault must not
# become N stampeding fetches. Entries are removed in the leader's `finally`,
# so the map holds at most one entry per vault being fetched right now.
_pending: dict[_CacheKey, asyncio.Future] = {}

# Any invalidation makes an in-flight fetch ineligible for storage.  A global
# epoch intentionally over-invalidates unrelated concurrent misses: updates
# are rare, while storing stale owner instructions after an update/delete is
# a correctness and trust-boundary failure.
_invalidation_epoch = 0


def reset() -> None:
    """Clear all state and re-read the configured bounds.

    Called by tests, and available to startup. Bounds are re-read here rather
    than per call so the hot path stays plain global reads — the same trade
    `tool_usage.reset()` makes.
    """
    global _BODY_MAX, _CACHE_TTL, _SESSION_MAP_MAX, _VAULT_CACHE_MAX
    global _FETCH_TIMEOUT, _last_timeout_log, _invalidation_epoch
    _BODY_MAX = settings.vault_skill.body_max_bytes
    _CACHE_TTL = settings.vault_skill.cache_ttl_secs
    _SESSION_MAP_MAX = settings.vault_skill.session_map_max
    _VAULT_CACHE_MAX = settings.vault_skill.vault_cache_max
    _FETCH_TIMEOUT = settings.vault_skill.fetch_timeout_secs
    _last_timeout_log = 0.0
    _invalidation_epoch = 0
    _vault_cache.clear()
    _session_map.clear()
    _acknowledged_map.clear()
    _challenge_map.clear()
    _pending.clear()


def invalidate(vault: str) -> None:
    """Write-through hook: a commit landed on the canonical path, or the vault
    itself was deleted. Callers MUST be past their commit — see module docs."""
    global _invalidation_epoch
    _invalidation_epoch += 1
    for cache_key in [key for key in _vault_cache if key[0] == vault]:
        _vault_cache.pop(cache_key, None)
    # A challenge for the previous body must not remain usable after a write,
    # delete, or same-name recreation.  Acknowledged versions are harmless
    # because the next strict preflight compares them with the fresh version.
    for challenge_key in [key for key in _challenge_map if key[1] == vault]:
        _challenge_map.pop(challenge_key, None)


def _remember(
    mapping: OrderedDict[tuple[str, str, str | None], str],
    key: tuple[str, str, str | None],
    value: str,
) -> None:
    """Store one per-session value and preserve the configured LRU bound."""
    mapping[key] = value
    mapping.move_to_end(key)
    while len(mapping) > _SESSION_MAP_MAX:
        mapping.popitem(last=False)


def _remember_challenge(
    key: tuple[str, str, str | None], version: str, token: str
) -> None:
    _challenge_map[key] = (version, token)
    _challenge_map.move_to_end(key)
    while len(_challenge_map) > _SESSION_MAP_MAX:
        _challenge_map.popitem(last=False)


def _format_payload(
    vault: str,
    version: str,
    body: str,
    *,
    updated: bool,
    ack_token: str | None = None,
) -> dict:
    truncated = len(body.encode("utf-8")) > _BODY_MAX
    if truncated:
        clipped = body.encode("utf-8")[:_BODY_MAX].decode("utf-8", "ignore")
        body = (
            clipped
            + "\n\n[truncated — call akb_help(topic=\"vault-skill\", vault=\""
            + vault
            + "\") for the full text]"
        )
    payload = {
        "vault": vault,
        "version": version,
        "reason": "updated" if updated else "first_touch",
        "body": body,
        "truncated": truncated,
    }
    if ack_token is not None:
        payload["ack_token"] = ack_token
    return payload


async def _fetch_skill(
    vault: str, expected_vault_id: str | None = None, *, documents=None
) -> dict | None:
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
    vault_repo = VaultRepository(pool)
    vault_id = await vault_repo.get_id_by_name(vault)
    if vault_id is None:
        return None
    if expected_vault_id is not None and str(vault_id) != expected_vault_id:
        return None
    if await VaultExternalGitRepository(pool).exists(vault_id):
        return None

    try:
        service = documents or get_document_service()
        doc = await service.get(vault, skill_policy.VAULT_SKILL_PATH)
    except NotFoundError:
        return None

    # The document service addresses by name. Recheck after its async git/DB
    # read so delete+recreate between the first lookup and that read cannot
    # project the replacement vault through the old authorization.
    if expected_vault_id is not None:
        current_id = await vault_repo.get_id_by_name(vault)
        if current_id is None or str(current_id) != expected_vault_id:
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


def _store(key: _CacheKey, version: str | None, body: str | None) -> None:
    """Write a cache entry and hold the LRU bound."""
    _vault_cache[key] = (time.monotonic(), version, body)
    _vault_cache.move_to_end(key)
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


async def _current(
    vault: str, expected_vault_id: str | None = None
) -> tuple[str | None, str | None]:
    key = (vault, expected_vault_id)
    hit = _vault_cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        _vault_cache.move_to_end(key)
        return hit[1], hit[2]

    inflight = _pending.get(key)
    if inflight is not None:
        # `shield` so a cancelled FOLLOWER cannot cancel the shared fetch out
        # from under the other waiters (awaiting a Future directly would).
        return await asyncio.shield(inflight)

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[key] = fut
    fetch_epoch = _invalidation_epoch
    try:
        try:
            doc = await asyncio.wait_for(
                _fetch_skill(vault, expected_vault_id), timeout=_FETCH_TIMEOUT
            )
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
        if fetch_epoch != _invalidation_epoch:
            # A writer committed while this fetch was in flight.  The bytes
            # may describe either side of that commit, so serve/cache neither.
            result = (None, None)
        else:
            _store(key, result[0], result[1])
        fut.set_result(result)
        return result
    finally:
        _pending.pop(key, None)


async def injection_payload(
    session_id: str | None, vault: str, vault_id: str | None = None
) -> dict | None:
    """The additive/legacy ``vault_skill`` payload, or ``None``.

    The state snapshot is taken before the potentially blocking fetch.  Thus a
    cohort of concurrent legacy-v1 first writes all receives the guide instead
    of the first waiter marking it delivered for the rest.  A later sequential
    retry sees the stored version and proceeds under the v1 compatibility
    contract.  Capability v2 uses :func:`preflight_payload` instead.
    """
    try:
        if not settings.vault_skill.enabled:
            return None
        # Before any key is formed, in either map: an over-long name is
        # refused outright and caches nothing.
        if len(vault) > _VAULT_NAME_MAX:
            return None
        # Stdio/CLI callers have no session id; treat them as one
        # per-process pseudo-session (a stdio proxy is one conversation).
        key = (session_id or "no-session", vault, vault_id)
        # Capture before `_current()` awaits.  Do not re-read after the fetch:
        # another request from this same arrival cohort may have completed in
        # the meantime, but this request still has not delivered a guide.
        prev = _session_map.get(key)
        version, body = await _current(vault, vault_id)
        if version is None or body is None:
            return None
        if prev == version:
            _session_map.move_to_end(key)
            return None
        _remember(_session_map, key, version)
        return _format_payload(
            vault, version, body, updated=prev is not None
        )
    except Exception as e:  # noqa: BLE001 — injection must never fail a tool call
        logger.warning("vault_skill injection skipped for %s: %s", vault, e)
        return None


async def preflight_payload(
    session_id: str | None,
    vault: str,
    vault_id: str | None = None,
    *,
    acknowledgement: str | None = None,
) -> dict | None:
    """Return a strict write challenge until the current guide is acknowledged.

    No state is advanced merely because a response was constructed.  That is
    the concurrency invariant: every parallel first write without the opaque
    token remains non-mutating.  A token is valid only for the session/vault/id
    key that issued it and for the guide version that is still current.

    ``None`` means the session already acknowledged this version, or that the
    optional guide could not be projected.  Like additive injection, this
    helper never raises and performs no authorization itself.
    """
    try:
        if not settings.vault_skill.enabled:
            return None
        if len(vault) > _VAULT_NAME_MAX:
            return None
        key = (session_id or "no-session", vault, vault_id)
        version, body = await _current(vault, vault_id)
        if version is None or body is None:
            return None

        acknowledged = _acknowledged_map.get(key)
        if acknowledged == version:
            _acknowledged_map.move_to_end(key)
            return None

        challenge = _challenge_map.get(key)
        if challenge is None or challenge[0] != version:
            challenge = (version, secrets.token_urlsafe(24))
            _remember_challenge(key, challenge[0], challenge[1])
        else:
            _challenge_map.move_to_end(key)

        if (
            isinstance(acknowledgement, str)
            and len(acknowledgement) == len(challenge[1])
            and hmac.compare_digest(acknowledgement, challenge[1])
        ):
            _remember(_acknowledged_map, key, version)
            _remember(_session_map, key, version)
            _challenge_map.pop(key, None)
            return None

        return _format_payload(
            vault,
            version,
            body,
            updated=acknowledged is not None,
            ack_token=challenge[1],
        )
    except Exception as e:  # noqa: BLE001 — preflight must never fail a tool call
        logger.warning("vault_skill preflight skipped for %s: %s", vault, e)
        return None


async def fetch_for_authorized_reader(
    vault: str, vault_id: str, *, documents=None
) -> dict | None:
    """Fetch explicit-help content pinned to a completed reader check.

    This is deliberately the same identity- and mirror-aware read as automatic
    injection; explicit help must not become a weaker alternate channel.
    """
    return await _fetch_skill(vault, vault_id, documents=documents)
