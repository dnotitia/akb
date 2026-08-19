"""Vault-skill auto-injection state: version cache + per-session tracking.

`mcp_server/server.py` calls `injection_payload()` from the dispatch
chokepoint. Contract mirrors `tool_usage.record`: NEVER raises, never blocks
the tool call on failure — a lookup error is logged and skipped.

Session tracking is (session_id, vault) → last-injected skill version, so a
long-lived session re-receives the skill exactly when it changes. The vault
cache holds (version, body) with a short TTL plus write-through invalidation
from the document services (same-process; API replicas=1 today).

Mirror vaults are excluded even when the upstream repo carries an
overview/vault-skill.md: auto-injecting upstream-controlled markdown into
every agent session would be an instruction-injection vector. akb_help still
serves such a file on explicit request; only the automatic channel is closed.
"""

from __future__ import annotations

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

# vault → (fetched_at_monotonic, version | None, body | None). A (None, None)
# entry is a NEGATIVE cache: absent doc or mirror vault, so the repeated
# lookups a mirror-heavy tenant would otherwise pay are amortized too.
_vault_cache: dict[str, tuple[float, str | None, str | None]] = {}
# (session_id, vault) → injected version. OrderedDict as LRU.
_session_map: OrderedDict[tuple[str, str], str] = OrderedDict()


def reset() -> None:
    """Clear all state and re-read the configured bounds.

    Called by tests, and available to startup. Bounds are re-read here rather
    than per call so the hot path stays plain global reads — the same trade
    `tool_usage.reset()` makes.
    """
    global _BODY_MAX, _CACHE_TTL, _SESSION_MAP_MAX
    _BODY_MAX = settings.vault_skill.body_max_bytes
    _CACHE_TTL = settings.vault_skill.cache_ttl_secs
    _SESSION_MAP_MAX = settings.vault_skill.session_map_max
    _vault_cache.clear()
    _session_map.clear()


def invalidate(vault: str) -> None:
    """Write-through hook: a commit landed on the canonical path."""
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


async def _current(vault: str) -> tuple[str | None, str | None]:
    now = time.monotonic()
    hit = _vault_cache.get(vault)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1], hit[2]
    doc = await _fetch_skill(vault)
    if doc is None:
        _vault_cache[vault] = (now, None, None)
        return None, None
    _vault_cache[vault] = (now, doc["version"], doc["content"])
    return doc["version"], doc["content"]


async def injection_payload(session_id: str | None, vault: str) -> dict | None:
    """The `vault_skill` payload to attach, or None. Never raises."""
    try:
        if not settings.vault_skill.enabled:
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
