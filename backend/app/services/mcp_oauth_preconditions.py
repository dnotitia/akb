"""Realm-side preconditions for the MCP OAuth Resource Server path.

``config.py`` already refuses ``mcp_oauth_enabled=true`` with
``keycloak_enabled=false``, because that one is decidable from AKB's own
configuration. The other half of the same deployment — whether the realm
was ever given the client scopes and the DCR policy the path needs — is
not in AKB's config at all, and until this module existed AKB had no
opinion about it. A realm that was never set up fails at the IdP, before
any AKB code runs: the client gets a bare ``403`` naming a Keycloak
policy, and nothing in AKB says which script would fix it.

**Where this runs, and why it is not at startup.** This is a ``/health``
section, not a boot check. A boot check would have to be non-fatal (the
IdP can be briefly unreachable while AKB comes up, and MCP OAuth must
never become a boot dependency), and a non-fatal boot check that could
not reach the IdP is a check that silently never ran — with no second
chance for the rest of the process lifetime. On ``/health`` the probe
re-evaluates: a transient IdP outage reports ``unknown`` and heals on the
next poll, and the answer is available at the moment an operator is
actually debugging a connector, which is when they reach for ``/health``.
The cost is bounded by the TTL and the probe timeout below.

**What it can and cannot see.** Everything here comes from the
authorization server's public discovery document, so it covers exactly
two of the four walls an unconfigured realm puts up: whether DCR is
offered at all (``registration_endpoint``) and whether the two vault
scopes exist (``scopes_supported``). The audience mapper on those scopes
and the DCR policy configuration are admin-API state, invisible from
discovery — so ``ok`` here means "the preconditions visible in discovery
hold", never "the realm is fully configured". The report says so rather
than implying more than it checked.

**Auth posture.** This section sits on the unauthenticated half of
``/health``. Every input is already public: the protected-resource
metadata names the authorization server to any caller, and its discovery
document is public by OIDC Discovery. So there is no marginal disclosure
here, only a statement about documents the caller can already read.
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

# The scopes `/.well-known/oauth-protected-resource` advertises and the
# consent screen has to be able to offer. Keycloak lists a realm client
# scope in `scopes_supported` once it is a default or optional scope,
# which is the state step 3 of the setup script leaves behind.
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
    """Classify one discovery document. Pure — no I/O, no settings.

    ``missing`` is what the document positively lacks; ``unverifiable``
    is what it does not say enough about to judge. The distinction
    matters: ``scopes_supported`` is only RECOMMENDED by OIDC Discovery,
    so an authorization server that omits it is not misconfigured, it is
    merely unreadable from here — reporting that as a missing scope would
    be a false alarm pointing at a setup script that would not help.
    """
    if not isinstance(document, dict):
        return {
            "status": "unknown",
            "missing": [],
            "unverifiable": ["registration_endpoint", *(f"scope:{s}" for s in REQUIRED_SCOPES)],
            "reason": "discovery document unavailable or not a JSON object",
        }

    missing: list[str] = []
    unverifiable: list[str] = []

    registration = document.get("registration_endpoint")
    if not (isinstance(registration, str) and registration.strip()):
        missing.append("registration_endpoint")

    advertised = document.get("scopes_supported")
    if isinstance(advertised, list):
        known = {item for item in advertised if isinstance(item, str)}
        missing.extend(f"scope:{scope}" for scope in REQUIRED_SCOPES if scope not in known)
    else:
        unverifiable.extend(f"scope:{scope}" for scope in REQUIRED_SCOPES)

    if missing:
        status = "unconfigured"
    elif unverifiable:
        status = "unknown"
    else:
        status = "ok"
    return {"status": status, "missing": missing, "unverifiable": unverifiable, "reason": ""}


def describe(report: dict) -> str:
    """The operator-facing sentence for one report. Names the fix."""
    status = report.get("status")
    if status == "ok":
        return (
            "The authorization server advertises a registration endpoint and both vault "
            "scopes. The audience mapper and the DCR registration policies are admin-API "
            f"state and are not visible here — see {RUNBOOK} if registration still fails."
        )
    if status == "unconfigured":
        absent = ", ".join(report.get("missing") or ())
        return (
            f"The authorization server does not advertise: {absent}. MCP clients cannot "
            f"register or obtain usable tokens until the realm is configured. Run "
            f"{SETUP_SCRIPT} against this realm, or follow the hand-edit checklist in "
            f"{RUNBOOK}."
        )
    reason = report.get("reason") or "the discovery document did not answer"
    return (
        f"Realm preconditions could not be checked ({reason}); this is not a verdict on "
        f"the realm. If MCP clients are failing to register, {SETUP_SCRIPT} is what "
        f"configures the realm and {RUNBOOK} documents the same steps by hand."
    )


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
