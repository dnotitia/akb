"""Cached public discovery checks for the optional native OAuth client.

Discovery cannot prove a client registration, signed audience or rotation.
The operator script verifies admin state. Real code/PKCE acceptance verifies
issued tokens. An `ok` health result describes public discovery only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.services import http_pool

logger = logging.getLogger("akb.mcp_oauth")

# Named in every remediation string. An operator reading a warning should
# not have to search the repository for the thing that fixes it.
SETUP_SCRIPT = "scripts/keycloak/setup-akb-mcp-oauth.py"
RUNBOOK = "docs/mcp-clients/web-connectors.md"

REQUIRED_SCOPES = ("akb:vault:read", "akb:vault:write")

# A settled answer is cheap to keep: realm configuration changes on
# operator time, not request time. An unsettled one is re-probed sooner
# so a brief IdP outage clears on the next poll rather than the next
# five minutes of polls.
_SETTLED_TTL_SECS = 300.0
_UNKNOWN_TTL_SECS = 30.0
# `/health` is polled by uptime checks; an unreachable IdP must cost a
# bounded pause, not a hung probe.
_PROBE_TIMEOUT_SECS = 2.0

_lock = asyncio.Lock()
_cached: dict | None = None
_cached_at: float = 0.0
_warned: bool = False


def _now() -> float:
    """Indirection so a test can advance the TTL clock without patching
    ``time.monotonic`` out from under the event loop."""
    return time.monotonic()


def _copy(section: dict) -> dict:
    """Hand out a section the caller can annotate without rewriting the cache.

    A plain ``dict()`` would still share the two lists, so a caller that
    appended to ``missing`` would change what the next poller reads.
    """
    duplicate = dict(section)
    for key in ("missing", "unverifiable"):
        value = duplicate.get(key)
        if isinstance(value, list):
            duplicate[key] = list(value)
    return duplicate


def reset_cache() -> None:
    """Drop the memoized probe. For tests and for a deliberate re-probe."""
    global _cached, _cached_at, _warned
    _cached = None
    _cached_at = 0.0
    _warned = False


def evaluate_discovery(document: object) -> dict:
    if not isinstance(document, dict):
        return {"status": "unknown", "missing": [], "unverifiable": ["discovery"],
                "reason": "discovery document unavailable or not a JSON object"}
    missing: list[str] = []
    unverifiable: list[str] = []
    for name in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(document.get(name), str) or not document[name].strip():
            missing.append(name)
    for name, required in (("scopes_supported", REQUIRED_SCOPES),
                           ("grant_types_supported", ("authorization_code", "refresh_token")),
                           ("code_challenge_methods_supported", ("S256",))):
        values = document.get(name)
        if not isinstance(values, list):
            unverifiable.append(name)
        else:
            missing.extend(f"{name}:{value}" for value in required if value not in values)
    status = "unconfigured" if missing else "unknown" if unverifiable else "ok"
    return {"status": status, "missing": missing, "unverifiable": unverifiable, "reason": ""}


def describe(report: dict) -> str:
    if report.get("status") == "ok":
        return (
            "Public discovery advertises code, refresh, S256 and both vault scopes. "
            "The native registration, exact audience mapper and refresh rotation are "
            f"not visible here. Verify them with {SETUP_SCRIPT} and the code/PKCE flow in {RUNBOOK}."
        )
    if report.get("status") == "unconfigured":
        absent = ", ".join(report.get("missing") or ())
        return f"Public discovery lacks {absent}. Run {SETUP_SCRIPT}; see {RUNBOOK}."
    reason = report.get("reason") or "the discovery document did not answer"
    return f"OAuth preconditions are unknown ({reason}); this is not a verdict on the realm. See {SETUP_SCRIPT} and {RUNBOOK}."


async def _fetch_discovery() -> object | None:
    """Read the authorization server's discovery document, or ``None``."""
    try:
        response = await http_pool.get_client().get(
            settings.keycloak_discovery_url, timeout=_PROBE_TIMEOUT_SECS
        )
        if response.status_code != 200:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        # Unreachable, slow, or not JSON. All three are "cannot tell",
        # and none of them is evidence that the realm is unconfigured.
        return None


async def health_section() -> dict:
    """The ``mcp_oauth`` section of ``/health``.

    Returns the memoized verdict when it is still fresh. The lock is held
    across the probe on purpose: concurrent ``/health`` callers coalesce
    onto one outbound request instead of stampeding the IdP.
    """
    global _cached, _cached_at, _warned

    if not settings.mcp_oauth_enabled:
        # Reported rather than omitted so a consumer can tell "this
        # deployment has MCP OAuth off" from "this build predates the check".
        return {"enabled": False}

    now = _now()
    async with _lock:
        if _cached is not None:
            ttl = _UNKNOWN_TTL_SECS if _cached.get("status") == "unknown" else _SETTLED_TTL_SECS
            if now - _cached_at < ttl:
                return _copy(_cached)

        report = evaluate_discovery(await _fetch_discovery())
        payload = {
            "enabled": True,
            "status": report["status"],
            "authorization_server": settings.keycloak_issuer,
            "native_client_id": settings.mcp_oauth_native_client_id,
            "registration_verified": False,
            "missing": report["missing"],
            "unverifiable": report["unverifiable"],
            "detail": describe(report),
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        _cached = payload
        _cached_at = _now()

        # One line per transition, not per poll: an operator greps the log
        # once at deploy time, and a warning repeated every poll interval
        # is the kind of noise that gets filtered and then missed.
        if payload["status"] == "unconfigured" and not _warned:
            _warned = True
            logger.warning("MCP OAuth realm preconditions unmet: %s", payload["detail"])
        elif payload["status"] == "ok" and _warned:
            _warned = False
            logger.info("MCP OAuth realm preconditions now satisfied for %s", payload["authorization_server"])

        return _copy(payload)
