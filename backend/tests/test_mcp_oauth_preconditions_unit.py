"""The realm-side preconditions AKB can observe, and the ones it cannot.

A realm that never had the MCP OAuth setup applied fails at the IdP with a
bare 403 before any AKB code runs, and AKB used to have no opinion about
it (akb#635). `mcp_oauth_preconditions` reads the authorization server's
discovery document and reports what it found on `/health`.

The distinctions these tests hold:

- `unconfigured` is a claim about the realm; `unknown` is a claim about
  reachability. Conflating them would either cry wolf at an IdP blip or
  stay silent about a realm that will never work.
- `scopes_supported` is only RECOMMENDED by OIDC Discovery, so its
  absence is `unknown`, never `unconfigured`.
- the remediation text names the setup script — that naming is the whole
  point of the feature, so it is asserted rather than assumed.
- the probe is memoized, and a `/health` poll must never stampede the IdP.
"""

from __future__ import annotations

import inspect
import logging

import httpx
import pytest

from app.config import settings
from app.services import mcp_oauth_preconditions as preconditions


CONFIGURED_REALM = {
    "issuer": "https://auth.example.com/realms/akb",
    "registration_endpoint": "https://auth.example.com/realms/akb/clients-registrations/openid-connect",
    "scopes_supported": ["openid", "profile", "email", "akb:vault:read", "akb:vault:write", "offline_access"],
}


@pytest.fixture(autouse=True)
def _clean_cache():
    preconditions.reset_cache()
    yield
    preconditions.reset_cache()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://auth.example.com", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)


def _answer(monkeypatch, document):
    """Make the probe return one document (or None for unreachable)."""
    calls: list[int] = []

    async def _fetch():
        calls.append(1)
        return document

    monkeypatch.setattr(preconditions, "_fetch_discovery", _fetch)
    return calls


# ── evaluate_discovery: pure classification ────────────────────────


def test_configured_realm_is_ok():
    report = preconditions.evaluate_discovery(CONFIGURED_REALM)
    assert report["status"] == "ok"
    assert report["missing"] == []
    assert report["unverifiable"] == []


def test_realm_without_dcr_endpoint_is_unconfigured():
    document = {k: v for k, v in CONFIGURED_REALM.items() if k != "registration_endpoint"}
    report = preconditions.evaluate_discovery(document)
    assert report["status"] == "unconfigured"
    assert report["missing"] == ["registration_endpoint"]


def test_blank_dcr_endpoint_counts_as_absent():
    report = preconditions.evaluate_discovery({**CONFIGURED_REALM, "registration_endpoint": "   "})
    assert report["status"] == "unconfigured"
    assert "registration_endpoint" in report["missing"]


def test_missing_vault_scopes_are_named_individually():
    document = {**CONFIGURED_REALM, "scopes_supported": ["openid", "akb:vault:read"]}
    report = preconditions.evaluate_discovery(document)
    assert report["status"] == "unconfigured"
    assert report["missing"] == ["scope:akb:vault:write"]


def test_a_never_configured_realm_names_everything_that_is_absent():
    report = preconditions.evaluate_discovery({"issuer": "https://auth.example.com/realms/akb",
                                               "scopes_supported": ["openid", "profile", "email"]})
    assert report["status"] == "unconfigured"
    assert report["missing"] == [
        "registration_endpoint",
        "scope:akb:vault:read",
        "scope:akb:vault:write",
    ]


def test_an_idp_that_omits_scopes_supported_is_unknown_not_unconfigured():
    # OIDC Discovery makes `scopes_supported` RECOMMENDED, not REQUIRED.
    # Calling that a missing scope would point an operator at a setup
    # script that would not change the document.
    document = {k: v for k, v in CONFIGURED_REALM.items() if k != "scopes_supported"}
    report = preconditions.evaluate_discovery(document)
    assert report["status"] == "unknown"
    assert report["missing"] == []
    assert report["unverifiable"] == ["scope:akb:vault:read", "scope:akb:vault:write"]


@pytest.mark.parametrize("document", [None, "not json", 42, ["a", "list"]])
def test_an_unreadable_document_is_unknown(document):
    report = preconditions.evaluate_discovery(document)
    assert report["status"] == "unknown"
    assert report["missing"] == []


# ── describe: the remediation an operator acts on ──────────────────


def test_unconfigured_detail_names_the_setup_script_and_what_is_missing():
    detail = preconditions.describe(preconditions.evaluate_discovery({"scopes_supported": []}))
    assert preconditions.SETUP_SCRIPT in detail
    assert "registration_endpoint" in detail
    assert "akb:vault:read" in detail


def test_unknown_detail_still_names_the_script_but_does_not_accuse_the_realm():
    detail = preconditions.describe(preconditions.evaluate_discovery(None))
    assert preconditions.SETUP_SCRIPT in detail
    assert "not a verdict" in detail


def test_ok_detail_does_not_claim_more_than_discovery_can_show():
    detail = preconditions.describe(preconditions.evaluate_discovery(CONFIGURED_REALM))
    # The audience mapper and the DCR policies are admin-API state.
    assert "not visible here" in detail


# ── health_section: the /health surface ────────────────────────────


@pytest.mark.asyncio
async def test_disabled_deployment_reports_disabled_and_never_touches_the_idp(monkeypatch):
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False, raising=False)
    calls = _answer(monkeypatch, CONFIGURED_REALM)
    assert await preconditions.health_section() == {"enabled": False}
    assert calls == []


@pytest.mark.asyncio
async def test_configured_realm_reports_ok_without_warning(enabled, monkeypatch, caplog):
    _answer(monkeypatch, CONFIGURED_REALM)
    with caplog.at_level(logging.WARNING, logger="akb.mcp_oauth"):
        section = await preconditions.health_section()
    assert section["enabled"] is True
    assert section["status"] == "ok"
    assert section["authorization_server"] == "https://auth.example.com/realms/akb"
    assert section["checked_at"].endswith("Z")
    assert caplog.records == []


@pytest.mark.asyncio
async def test_unconfigured_realm_reports_and_warns_once(enabled, monkeypatch, caplog):
    _answer(monkeypatch, {"scopes_supported": []})
    with caplog.at_level(logging.WARNING, logger="akb.mcp_oauth"):
        first = await preconditions.health_section()
        second = await preconditions.health_section()
    assert first["status"] == "unconfigured"
    assert preconditions.SETUP_SCRIPT in first["detail"]
    assert second == first
    # One line per transition — a warning repeated on every poll gets
    # filtered, and then missed.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert preconditions.SETUP_SCRIPT in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_unreachable_idp_is_unknown_not_a_verdict(enabled, monkeypatch, caplog):
    _answer(monkeypatch, None)
    with caplog.at_level(logging.WARNING, logger="akb.mcp_oauth"):
        section = await preconditions.health_section()
    assert section["status"] == "unknown"
    # An IdP blip must not be logged as a misconfigured realm.
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_settled_answer_is_memoized_across_polls(enabled, monkeypatch):
    calls = _answer(monkeypatch, CONFIGURED_REALM)
    for _ in range(5):
        await preconditions.health_section()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_is_re_probed_sooner_than_a_settled_answer(enabled, monkeypatch):
    # A brief IdP outage must clear on the next poll, not five minutes of
    # them. Half the settled TTL is the probe point: inside it a settled
    # answer is still served, an unknown one must already have expired.
    assert preconditions._UNKNOWN_TTL_SECS < preconditions._SETTLED_TTL_SECS / 2
    clock = [1000.0]
    monkeypatch.setattr(preconditions, "_now", lambda: clock[0])
    calls = _answer(monkeypatch, None)

    await preconditions.health_section()
    await preconditions.health_section()
    assert len(calls) == 1, "still inside the unknown TTL"

    clock[0] += preconditions._SETTLED_TTL_SECS / 2
    await preconditions.health_section()
    assert len(calls) == 2, "an unknown answer must expire in well under the settled TTL"


@pytest.mark.asyncio
async def test_a_settled_answer_is_kept_for_the_settled_ttl(enabled, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(preconditions, "_now", lambda: clock[0])
    calls = _answer(monkeypatch, CONFIGURED_REALM)

    await preconditions.health_section()
    clock[0] += preconditions._SETTLED_TTL_SECS / 2
    await preconditions.health_section()
    assert len(calls) == 1

    clock[0] += preconditions._SETTLED_TTL_SECS
    await preconditions.health_section()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_persistently_unconfigured_realm_warns_once_not_every_probe(
    enabled, monkeypatch, caplog
):
    clock = [1000.0]
    monkeypatch.setattr(preconditions, "_now", lambda: clock[0])
    _answer(monkeypatch, {"scopes_supported": []})

    with caplog.at_level(logging.WARNING, logger="akb.mcp_oauth"):
        for _ in range(3):
            await preconditions.health_section()
            clock[0] += preconditions._SETTLED_TTL_SECS + 1

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.asyncio
async def test_recovery_clears_the_warned_state_so_a_regression_warns_again(
    enabled, monkeypatch, caplog
):
    clock = [1000.0]
    monkeypatch.setattr(preconditions, "_now", lambda: clock[0])
    document: list[object] = [{"scopes_supported": []}]

    async def _fetch():
        return document[0]

    monkeypatch.setattr(preconditions, "_fetch_discovery", _fetch)

    with caplog.at_level(logging.INFO, logger="akb.mcp_oauth"):
        await preconditions.health_section()
        document[0] = CONFIGURED_REALM
        clock[0] += preconditions._SETTLED_TTL_SECS + 1
        await preconditions.health_section()
        document[0] = {"scopes_supported": []}
        clock[0] += preconditions._SETTLED_TTL_SECS + 1
        await preconditions.health_section()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_a_returned_section_cannot_mutate_the_cache(enabled, monkeypatch):
    # Both exits hand out a copy: the fresh probe and the cache hit. A
    # caller that annotates the section it got must not rewrite what the
    # next poller reads.
    _answer(monkeypatch, CONFIGURED_REALM)
    fresh = await preconditions.health_section()
    fresh["status"] = "tampered-fresh"
    fresh["missing"].append("tampered-fresh")
    cached = await preconditions.health_section()
    assert cached["status"] == "ok"
    assert cached["missing"] == []
    cached["status"] = "tampered-cached"
    cached["missing"].append("tampered-cached")
    again = await preconditions.health_section()
    assert again["status"] == "ok"
    assert again["missing"] == []


# ── _fetch_discovery: failure modes are all "cannot tell" ──────────


class _StubClient:
    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.timeouts: list[float] = []

    async def get(self, url, timeout=None):
        self.timeouts.append(timeout)
        return self._behaviour(url)


@pytest.mark.asyncio
async def test_fetch_uses_the_backchannel_discovery_url_with_a_bounded_timeout(
    enabled, monkeypatch
):
    seen: list[str] = []

    def _ok(url):
        seen.append(url)
        return httpx.Response(200, json=CONFIGURED_REALM)

    stub = _StubClient(_ok)
    monkeypatch.setattr(preconditions.http_pool, "get_client", lambda: stub)
    assert await preconditions._fetch_discovery() == CONFIGURED_REALM
    assert seen == [settings.keycloak_discovery_url]
    assert seen[0].endswith("/realms/akb/.well-known/openid-configuration")
    # `/health` is polled by uptime checks: an unreachable IdP must cost a
    # bounded pause, not a hung probe.
    assert stub.timeouts == [preconditions._PROBE_TIMEOUT_SECS]


@pytest.mark.asyncio
async def test_non_200_discovery_is_unreadable(enabled, monkeypatch):
    # A JSON body on a non-200 is the case worth pinning: an IdP error
    # page or a proxy's JSON 404 parses fine, so only the status check
    # keeps it from being read as a realm with nothing configured.
    stub = _StubClient(lambda url: httpx.Response(404, json={"error": "Realm does not exist"}))
    monkeypatch.setattr(preconditions.http_pool, "get_client", lambda: stub)
    assert await preconditions._fetch_discovery() is None


@pytest.mark.asyncio
async def test_transport_error_is_unreadable_rather_than_an_exception(enabled, monkeypatch):
    def _boom(url):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(preconditions.http_pool, "get_client", lambda: _StubClient(_boom))
    assert await preconditions._fetch_discovery() is None


@pytest.mark.asyncio
async def test_non_json_body_is_unreadable_rather_than_an_exception(enabled, monkeypatch):
    stub = _StubClient(lambda url: httpx.Response(200, text="<html>login</html>"))
    monkeypatch.setattr(preconditions.http_pool, "get_client", lambda: stub)
    assert await preconditions._fetch_discovery() is None


# ── wiring ─────────────────────────────────────────────────────────


def test_health_route_reports_the_section():
    """A source-level assertion, deliberately.

    Calling the `/health` handler needs a database and a vector-store
    driver, which is a disproportionate fixture for one key. What can
    silently regress here is the wiring — the section being computed and
    then not returned — so that is what this pins.
    """
    from app import main

    source = inspect.getsource(main.health)
    assert '"mcp_oauth": await _safe(mcp_oauth_preconditions.health_section)' in source
