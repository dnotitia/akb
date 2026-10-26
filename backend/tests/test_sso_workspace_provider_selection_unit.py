"""Exercise workspace provider selection through real browser routes and ID verification.

Only external boundaries are replaced: transient storage, Keycloak's verified
token/code responses, and account/session persistence. Provider selection, the
browser binding, nonce/PKCE request, callback, and ID-token checks are real.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from app.api.routes import auth
from app.config import settings
from app.services import keycloak_oidc, sso_browser_session_service
from app.services.auth_service import AuthenticatedUser, ProjectionOutcome
from app.services.auth_verifier_profiles import VerifiedPrincipal
from app.services.keycloak_oidc import KeycloakOIDC
from app.services.sso_browser_session_service import IssuedSsoBrowserSession
from app.sso.keycloak_admin import KeycloakAdminConfig, KeycloakProviderControl, ProviderControlError
from app.sso.models import ProviderReadback


_ORIGIN = "https://akb.example.com"
_REDIRECT = "/vaults?selected=one"


def _provider(alias: str, state: str = "enabled") -> ProviderReadback:
    return ProviderReadback(
        provider_type="oidc", alias=alias, display_name=alias,
        state=state, enabled=state == "enabled",  # type: ignore[arg-type]
        issuer="https://upstream.example", discovery_url="https://upstream.example/discovery",
        client_id="broker-client", client_secret_configured=True,
        redirect_uri=f"https://id.example/realms/akb/broker/{alias}/endpoint",
        post_logout_redirect_uri=f"https://id.example/realms/akb/broker/{alias}/endpoint/logout_response",
        supports_logout=True, supports_identity_migration=False,
    )


class _BrowserFlow:
    def __init__(self, monkeypatch):
        self.providers = [
            _provider("workforce"), _provider("teams"),
            _provider("disabled", "configured_disabled"), _provider("broken", "configuration_error"),
        ]
        self.catalog_unavailable = False
        self.catalog_reads = []
        self.states = {}
        self.last_state = None
        self.last_payload = None
        self.access_provider = "workforce"
        self.id_provider = "workforce"
        self.id_claim_changes = {}
        self.projection_aliases = []
        self.session_aliases = []
        self.code_exchanges = []
        self.id_verification_aliases = []
        self.tokens = {
            "token_type": "Bearer", "access_token": "verified-access-value",
            "id_token": "verified-id-value", "refresh_token": "custodied-refresh-value",
            "refresh_expires_in": 3600,
        }
        self.service = KeycloakOIDC()

        async def list_providers(**kwargs):
            self.catalog_reads.append(kwargs)
            if self.catalog_unavailable:
                raise ProviderControlError("catalog-unavailable")
            return tuple(self.providers)

        class Control:
            control_mode = "direct"

        control = Control()
        control.list_providers = list_providers

        async def issue(state, kind, payload, *, ttl_secs):
            assert kind == "browser-state-v1"
            assert ttl_secs == 600
            self.last_state = state
            self.last_payload = copy.deepcopy(payload)
            self.states[state] = copy.deepcopy(payload)

        async def consume(state, binding_hash):
            payload = self.states.get(state)
            if payload is None or payload["browser_binding_hash"] != binding_hash:
                return None
            return self.states.pop(state)

        class TokenResponse:
            status_code = 200

            def json(inner):
                return self.tokens

        class TokenClient:
            async def post(inner, url, *, data):
                assert data["grant_type"] == "authorization_code"
                assert data["code"] == "keycloak-code"
                assert data["client_id"] == "akb-web"
                assert data["redirect_uri"] == _ORIGIN + "/api/v1/auth/keycloak/callback"
                assert data["code_verifier"] == self.last_payload["code_verifier"]
                self.code_exchanges.append(dict(data))
                return TokenResponse()

        async def verify_access(token, audience, *, route_profile):
            assert (token, audience, route_profile) == (
                self.tokens["access_token"], _ORIGIN + "/api", "api",
            )
            return VerifiedPrincipal(
                profile_id="keycloak-access-v1", issuer=settings.keycloak_issuer,
                subject="subject-1", credential_type="access_token",
                claims=self.claims(self.access_provider), audience=audience,
            )

        async def verify_id(token, *, client_id):
            assert token == self.tokens["id_token"]
            assert client_id == "akb-web"
            return {**self.claims(self.id_provider), **self.id_claim_changes}

        verify_browser_id = self.service.verify_browser_id_token

        async def observe_id_verification(token, **kwargs):
            self.id_verification_aliases.append(kwargs["expected_provider_alias"])
            return await verify_browser_id(token, **kwargs)

        user = AuthenticatedUser(
            user_id="00000000-0000-0000-0000-000000000001", username="alice",
            email="alice@example.com", display_name="Alice", is_admin=False, auth_method="oauth",
        )

        async def project(principal, *, provider_alias):
            self.projection_aliases.append(provider_alias)
            return ProjectionOutcome(user)

        async def create_session(value, principal, id_claims, tokens, *, login_sequence=None):
            assert value is user
            assert tokens == self.tokens
            assert login_sequence == 73
            assert principal.claims.get("identity_provider") == id_claims.get("identity_provider")
            actual = principal.claims.get("identity_provider", "local")
            self.session_aliases.append(actual)
            return IssuedSsoBrowserSession(
                token="opaque-browser-session-value", csrf_token="opaque-browser-csrf-value",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

        monkeypatch.setattr(auth, "get_keycloak_provider_control", lambda: control)
        monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: self.service)
        monkeypatch.setattr(auth, "sso_browser_session_ready", lambda: True)
        monkeypatch.setattr(auth, "project_verified_principal_with_reason", project)
        monkeypatch.setattr(auth, "create_sso_browser_session", create_session)
        monkeypatch.setattr(keycloak_oidc, "_store_issue", issue)
        monkeypatch.setattr(keycloak_oidc, "_store_consume_browser_bound", consume)
        monkeypatch.setattr(self.service, "_client", lambda: TokenClient())
        monkeypatch.setattr(self.service, "verify_access_token", verify_access)
        monkeypatch.setattr(self.service, "verify_id_token", verify_id)
        monkeypatch.setattr(self.service, "verify_browser_id_token", observe_id_verification)
        monkeypatch.setattr(
            sso_browser_session_service, "next_browser_login_sequence", AsyncMock(return_value=73),
        )
        self.app = FastAPI()
        self.app.include_router(auth.router, prefix="/api/v1")

    def claims(self, provider):
        digest = hashlib.sha256(self.tokens["access_token"].encode("ascii")).digest()
        claims = {
            "iss": settings.keycloak_issuer, "sub": "subject-1", "sid": "session-1",
            "azp": "akb-web", "nonce": self.last_payload["nonce"],
            "at_hash": keycloak_oidc._encode_base64url(digest[: len(digest) // 2]),
            "scope": "openid profile email", "iat": 100, "exp": 200,
        }
        if provider is not None:
            claims["identity_provider"] = provider
        return claims

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url=_ORIGIN, follow_redirects=False,
        )

    async def start(self, client, alias="local"):
        response = await client.get(f"/api/v1/auth/sso/{alias}/login", params={"redirect": _REDIRECT})
        assert response.status_code == 303
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["client_id"] == ["akb-web"]
        assert query["redirect_uri"] == [_ORIGIN + "/api/v1/auth/keycloak/callback"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["max_age"] == ["0"]
        assert "prompt" not in query
        if alias == "local":
            assert "kc_idp_hint" not in query
        else:
            assert query["kc_idp_hint"] == [alias]
        return query["state"][0]

    async def finish(self, client, state, **kwargs):
        return await client.get(
            "/api/v1/auth/keycloak/callback", params={"state": state, "code": "keycloak-code"}, **kwargs,
        )

    def assert_refused(self, response):
        assert response.status_code == 303
        assert response.headers["location"] == "/auth?sso_error=sso_failed"
        assert not self.projection_aliases
        assert not self.session_aliases
        assert not any("opaque-browser-session-value" in value for value in response.headers.get_list("set-cookie"))
        assert all(value not in response.text for value in self.tokens.values() if isinstance(value, str))


@pytest.fixture
def flow(monkeypatch):
    for name, value in {
        "auth_mode": "sso", "keycloak_enabled": True,
        "keycloak_server_url": "https://id.example", "keycloak_realm": "akb",
        "keycloak_client_id": "akb-web", "keycloak_public_client": True,
        "api_oauth_audience": _ORIGIN + "/api", "public_base_url": _ORIGIN,
        "sso_local_realm_login_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return _BrowserFlow(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", ["teams", "workforce", None])
async def test_workspace_choice_uses_actual_provider_for_verification_and_projection(flow, actual):
    flow.access_provider = flow.id_provider = actual
    async with flow.client() as client:
        state = await flow.start(client)
        allowed = flow.last_payload["allowed_provider_aliases"]
        assert set(allowed) == {"local", "workforce", "teams"}
        assert len(allowed) == 3
        response = await flow.finish(client, state)

    selected = actual or "local"
    assert response.status_code == 303
    assert response.headers["location"] == _REDIRECT
    assert flow.id_verification_aliases == [selected]
    assert flow.projection_aliases == [selected]
    assert flow.session_aliases == [selected]
    assert len(flow.code_exchanges) == 1
    expected_reads = [{"allow_stale": False}]
    if actual is not None:
        expected_reads.append({"allow_stale": False, "force_refresh": True})
    assert flow.catalog_reads == expected_reads
    cookies = response.headers.get_list("set-cookie")
    session = next(value for value in cookies if value.startswith("__Host-akb_sso_session="))
    assert "HttpOnly" in session and "Secure" in session and "Path=/" in session and "Domain=" not in session
    assert any(value.startswith("__Host-akb_sso_csrf=") and "HttpOnly" not in value for value in cookies)
    assert any(value.startswith("__Host-akb_sso_oidc_binding=") and "Max-Age=0" in value for value in cookies)
    assert response.headers["cache-control"] == "no-store"
    assert all(value not in "\n".join(cookies) for value in flow.tokens.values() if isinstance(value, str))


@pytest.mark.asyncio
async def test_direct_provider_preserves_the_legacy_state_format(flow):
    async with flow.client() as client:
        state = await flow.start(client, "workforce")
        assert "allowed_provider_aliases" not in flow.last_payload
        response = await flow.finish(client, state)
    assert response.status_code == 303
    assert response.headers["location"] == _REDIRECT
    assert flow.projection_aliases == ["workforce"]


@pytest.mark.asyncio
async def test_browser_query_cannot_choose_an_admin_client_or_broaden_a_direct_login(flow):
    async with flow.client() as client:
        started = await client.get("/api/v1/auth/sso/workforce/login", params={
            "redirect": _REDIRECT, "client_id": "akb-web-admin",
            "allowed_provider_aliases": "workforce,teams,local",
        })
        assert started.status_code == 303
        query = parse_qs(urlsplit(started.headers["location"]).query)
        assert query["client_id"] == ["akb-web"]
        assert flow.last_payload["client_id"] == "akb-web"
        assert "allowed_provider_aliases" not in flow.last_payload
        flow.access_provider = flow.id_provider = "teams"
        refused = await flow.finish(client, query["state"][0])
    flow.assert_refused(refused)


@pytest.mark.asyncio
async def test_workspace_callback_cannot_consume_an_admin_client_state(flow):
    async with flow.client() as client:
        state = await flow.start(client)
        flow.states[state]["client_id"] = "akb-web-admin"
        response = await flow.finish(client, state)
    flow.assert_refused(response)
    assert flow.code_exchanges == []


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,actual", [("workforce", "teams"), ("workforce", None)])
async def test_direct_provider_cannot_switch_even_to_an_enabled_provider(flow, selected, actual):
    async with flow.client() as client:
        state = await flow.start(client, selected)
        flow.access_provider = flow.id_provider = actual
        response = await flow.finish(client, state)
    flow.assert_refused(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,actual", [("local", "workforce"), ("workforce", "teams"), ("workforce", None)])
async def test_legacy_state_without_allowlist_retains_exact_provider_binding(flow, selected, actual):
    async with flow.client() as client:
        state = await flow.start(client, selected)
        flow.states[state].pop("allowed_provider_aliases", None)
        flow.access_provider = flow.id_provider = actual
        response = await flow.finish(client, state)
    flow.assert_refused(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,actual", [("local", None), ("workforce", "workforce")])
async def test_legacy_state_still_accepts_its_original_provider(flow, selected, actual):
    async with flow.client() as client:
        state = await flow.start(client, selected)
        flow.states[state].pop("allowed_provider_aliases", None)
        flow.access_provider = flow.id_provider = actual
        response = await flow.finish(client, state)
    assert response.status_code == 303
    assert response.headers["location"] == _REDIRECT
    assert flow.projection_aliases == [selected]


@pytest.mark.asyncio
async def test_provider_enabled_after_workspace_login_started_is_not_in_its_snapshot(flow):
    flow.providers = [_provider("workforce")]
    async with flow.client() as client:
        state = await flow.start(client)
        assert set(flow.last_payload["allowed_provider_aliases"]) == {"local", "workforce"}
        flow.providers.append(_provider("teams"))
        flow.access_provider = flow.id_provider = "teams"
        response = await flow.finish(client, state)
    flow.assert_refused(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("current_state", ["configured_disabled", "configuration_error", "removed"])
async def test_provider_disabled_or_removed_after_start_cannot_complete_workspace_login(flow, current_state):
    async with flow.client() as client:
        state = await flow.start(client)
        flow.providers = [] if current_state == "removed" else [_provider("workforce", current_state)]
        response = await flow.finish(client, state)
    flow.assert_refused(response)


@pytest.mark.asyncio
async def test_workspace_callback_refreshes_a_fresh_control_cache_before_admitting_a_provider(flow, monkeypatch):
    remote = [_provider("workforce")]
    remote_reads = []
    control = KeycloakProviderControl(KeycloakAdminConfig(
        internal_base_url="https://id.internal.example", public_base_url="https://id.example",
        realm="akb", management_client_id="fixture-manager",
        management_client_secret="fixture-only", verify_ssl=True,  # pragma: allowlist secret
    ), monotonic=lambda: 100.0)

    async def fetch_remote_catalog():
        remote_reads.append(tuple(provider.state for provider in remote))
        return tuple(remote)

    # Replace only the remote fetch. list_providers and all cache/failure-backoff
    # logic remain real; fixed monotonic time keeps its 15-second cache fresh.
    monkeypatch.setattr(control, "_fetch_catalog", fetch_remote_catalog)
    monkeypatch.setattr(auth, "get_keycloak_provider_control", lambda: control)
    assert (await control.list_providers(allow_stale=False))[0].state == "enabled"
    async with flow.client() as client:
        state = await flow.start(client)
        assert flow.last_payload["allowed_provider_aliases"] == ["local", "workforce"]
        assert remote_reads == [("enabled",)]
        remote[:] = [_provider("workforce", "configured_disabled")]
        # An ordinary fresh-cache read still sees the previous enabled result.
        assert (await control.list_providers(allow_stale=False))[0].state == "enabled"
        assert remote_reads == [("enabled",)]
        response = await flow.finish(client, state)

    flow.assert_refused(response)
    assert remote_reads == [("enabled",), ("configured_disabled",)]
    assert (await control.list_providers(allow_stale=False))[0].state == "configured_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", [None, "workforce"])
async def test_catalog_outage_preserves_native_workspace_login_without_allowing_external(flow, actual):
    flow.catalog_unavailable = True
    async with flow.client() as client:
        state = await flow.start(client)
        assert flow.last_payload["allowed_provider_aliases"] == ["local"]
        flow.access_provider = flow.id_provider = actual
        response = await flow.finish(client, state)
    if actual is None:
        assert response.headers["location"] == _REDIRECT
        assert flow.projection_aliases == ["local"]
    else:
        flow.assert_refused(response)


@pytest.mark.asyncio
async def test_catalog_outage_at_callback_refuses_a_previously_allowed_external_provider(flow):
    async with flow.client() as client:
        state = await flow.start(client)
        flow.catalog_unavailable = True
        response = await flow.finish(client, state)
    flow.assert_refused(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("access_provider,id_provider", [
    ("workforce", None), ("workforce", "teams"), (None, "workforce"),
])
async def test_workspace_callback_rejects_access_and_id_tokens_from_different_providers(
    flow, access_provider, id_provider,
):
    async with flow.client() as client:
        state = await flow.start(client)
        flow.access_provider = access_provider
        flow.id_provider = id_provider
        response = await flow.finish(client, state)
    flow.assert_refused(response)
    assert flow.id_verification_aliases == [access_provider or "local"]


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", [1, [], {}, "local", "../escape", "Workforce"])
async def test_invalid_signed_provider_claim_is_refused_without_issuing_a_session(flow, claim):
    async with flow.client() as client:
        state = await flow.start(client)
        flow.access_provider = flow.id_provider = claim
        response = await flow.finish(client, state)
    flow.assert_refused(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [
    None, "local", [], ["local", "local"], ["local", 1], ["local", "../escape"],
    ["local", "Workforce"], ["workforce"], ["local", {}],
    ["local", *(f"provider-{index}" for index in range(128))],
])
async def test_malformed_workspace_allowlist_is_rejected_before_code_exchange(flow, allowed):
    async with flow.client() as client:
        state = await flow.start(client)
        flow.states[state]["allowed_provider_aliases"] = allowed
        response = await flow.finish(client, state)
    flow.assert_refused(response)
    assert flow.code_exchanges == []


@pytest.mark.asyncio
async def test_direct_provider_state_cannot_be_broadened_into_a_chooser(flow):
    async with flow.client() as client:
        state = await flow.start(client, "workforce")
        flow.states[state]["allowed_provider_aliases"] = ["workforce", "teams"]
        flow.access_provider = flow.id_provider = "teams"
        response = await flow.finish(client, state)
    flow.assert_refused(response)
    assert flow.code_exchanges == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"nonce": "different-nonce-long-enough"}, {"at_hash": "wrong-access-token-hash"},
    {"azp": "akb-web-admin"}, {"iss": "https://untrusted.example/realms/akb"},
])
async def test_workspace_selection_preserves_actual_id_token_exchange_checks(flow, changes):
    async with flow.client() as client:
        state = await flow.start(client)
        flow.id_claim_changes = changes
        response = await flow.finish(client, state)
    flow.assert_refused(response)
    assert flow.id_verification_aliases == ["workforce"]


@pytest.mark.asyncio
async def test_workspace_state_remains_browser_bound_and_single_use(flow):
    async with flow.client() as client:
        state = await flow.start(client)
        async with flow.client() as other_browser:
            wrong_browser = await flow.finish(other_browser, state)
        flow.assert_refused(wrong_browser)
        assert flow.code_exchanges == []
        accepted = await flow.finish(client, state)
        assert accepted.headers["location"] == _REDIRECT
        replayed = await flow.finish(client, state)
    assert replayed.status_code == 303
    assert replayed.headers["location"] == "/auth?sso_error=sso_failed"
    assert flow.projection_aliases == ["workforce"]
    assert flow.session_aliases == ["workforce"]
    assert len(flow.code_exchanges) == 1
