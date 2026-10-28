"""The actual callback creates evidence atomically with an exact-bound session.

OIDC transport/verification is substituted; account projection, session custody,
challenge journal and event readback run against real isolated PostgreSQL.
"""
import base64
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx
import pytest

from app.api.routes import auth
from app.config import settings
from app.exceptions import AKBError
from app.services import sso_browser_session_service as sessions
from app.services.auth_verifier_profiles import VerifiedPrincipal
from app.sso import callback_evidence as evidence, identity_operations as ops, runtime_identity
from tests.test_sso_identity_operations_postgres import request, run
from tests.test_sso_management_targets_postgres import (  # noqa: F401
    preparation, lane, target_grant, switch_target, application, TARGET_ISSUER,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def browser(preparation, monkeypatch):  # noqa: F811
    cohort = preparation["spec"]["cohort"]
    grant = await target_grant(preparation, verbs=["apply", "postcheck", "enable", "login-evidence"], cohort=cohort)
    await run(preparation, grant, request("apply"))
    await run(preparation, grant, request("postcheck"))
    await run(preparation, grant, request("enable", payload={}))
    op_id = preparation["spec"]["operation_id"]
    challenge = await evidence.issue_challenge(op_id, grant["credential"], preparation["target"])
    raw = parse_qs(urlsplit(challenge["login_url"]).query)["login_challenge"][0]
    # Before the verifier switch a valid challenge still cannot start login.
    with pytest.raises(AKBError) as before:
        await evidence.browser_challenge(raw, "workforce")
    assert before.value.code == "sso_login_challenge_runtime_conflict"
    await switch_target(preparation, monkeypatch)
    monkeypatch.setattr(settings, "keycloak_client_id", "akb-web")
    monkeypatch.setattr(settings, "sso_browser_session_encryption_key", base64.urlsafe_b64encode(b"k"*32).decode().rstrip("="))
    monkeypatch.setattr(auth, "sso_browser_session_ready", lambda: True)
    async def pool():
        return preparation["pool"]
    monkeypatch.setattr(sessions, "get_pool", pool)
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {"iss": TARGET_ISSUER, "sub": "broker-subject", "sid": "verified-session",
              "identity_provider": "workforce", "scope": "openid profile email", "iat": now, "exp": now+300,
              "preferred_username": "target", "email": "target@example.test"}
    principal = VerifiedPrincipal(profile_id="keycloak-access-v1", issuer=TARGET_ISSUER, subject="broker-subject",
        credential_type="access_token", claims=claims, audience=settings.api_oauth_audience_effective)
    challenge_id = await evidence.browser_challenge(raw, "workforce")
    class VerifiedOIDC:
        valid = True
        state_challenge = challenge_id
        async def consume_browser_state(self, state, _binding):
            if state != "server-stored-state":
                return None
            transient = {"redirect_path": "/", "provider_alias": "workforce", "client_id": "akb-web",
                         "code_verifier": "server-verifier", "nonce": "server-nonce"}
            if self.state_challenge is not None:
                transient["login_challenge_id"] = self.state_challenge
            return transient
        async def exchange_browser_code(self, code, verifier):
            return {"token_type": "Bearer", "access_token": "fixture-access", "refresh_token": "fixture-refresh",
                    "id_token": "fixture-id", "refresh_expires_in": 600}
        async def verify_access_token(self, *_args, **_kwargs):
            return principal if self.valid else None
        async def verify_browser_id_token(self, *_args, **_kwargs):
            return claims
    oidc = VerifiedOIDC()
    monkeypatch.setattr(auth, "get_keycloak_oidc", lambda: oidc)
    diagnostics = []
    original_project = auth.project_verified_principal_with_reason
    async def project(*args, **kwargs):
        outcome = await original_project(*args, **kwargs)
        diagnostics.append(("projection", outcome.refusal_code))
        return outcome
    monkeypatch.setattr(auth, "project_verified_principal_with_reason", project)
    original_create = auth.create_sso_browser_session
    async def create(*args, **kwargs):
        try:
            return await original_create(*args, **kwargs)
        except AKBError as exc:
            diagnostics.append(("session", exc.code, str(exc)))
            raise
    monkeypatch.setattr(auth, "create_sso_browser_session", create)
    return {**preparation, "grant": grant, "challenge": challenge, "raw_challenge": raw,
            "op_id": op_id, "oidc": oidc, "diagnostics": diagnostics}


async def test_actual_verified_callback_atomic_session_event_and_exact_read(browser):
    pool = browser["pool"]
    assert await pool.fetchval("SELECT COUNT(*) FROM sso_callback_login_events") == 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="https://akb.example") as client:
        # Caller bytes have no event writer and cannot substitute server state.
        path = f"/api/v1/admin/sso/identity-operations/{browser['op_id']}/login-events"
        assert (await client.post(path, json={"event_id": str(uuid.uuid4())})).status_code == 405
        invalid = await client.get("/api/v1/auth/keycloak/callback", params={"code": "anything", "state": "caller-state",
            "login_challenge_id": browser["challenge"]["challenge_id"]})
        assert invalid.status_code == 303 and "sso_error" in invalid.headers["location"]
        assert await pool.fetchval("SELECT COUNT(*) FROM sso_callback_login_events") == 0
        response = await client.get("/api/v1/auth/keycloak/callback", params={"code": "verified-code", "state": "server-stored-state"})
        assert response.status_code == 303 and response.headers["location"] == "/", browser["diagnostics"]
        assert "__Host-akb_sso_session=" in response.headers.get("set-cookie", "")
        event = await pool.fetchrow("SELECT * FROM sso_callback_login_events")
        assert event["akb_user_id"] == browser["target"] and event["target_id"] == browser["target_id"]
        assert event["challenge_id"] == uuid.UUID(browser["challenge"]["challenge_id"])
        assert event["issuer"] == TARGET_ISSUER and event["sso_session_epoch"] == browser["target_epoch"]
        assert event["runtime_generation"] == 2 and event["source_revision"] == runtime_identity.source_revision()
        assert event["provider_revision"] == 2 and event["provider_secret_revision"] is None
        assert await pool.fetchval("SELECT COUNT(*) FROM sso_browser_sessions") == 1
        params = {"akb_user_id": str(browser["target"]), "challenge_id": browser["challenge"]["challenge_id"]}
        assert (await client.get(path, params=params)).status_code == 401
        pat = {"Authorization": "Bearer " + browser["raw_pat"]}
        read = await client.get(path, params=params, headers=pat)
        assert read.status_code == 200 and read.json()["events"][0]["event_id"] == str(event["event_id"])
        wrong = await client.get(path, params={**params, "challenge_id": str(uuid.uuid4())}, headers=pat)
        assert wrong.status_code == 200 and wrong.json()["events"] == []
        assert (await client.get(path, params={**params, "akb_user_id": str(browser["human"])}, headers=pat)).status_code == 403
        assert await pool.fetchval("SELECT COUNT(*) FROM sso_callback_login_events") == 1
        replay = await client.get("/api/v1/auth/keycloak/callback", params={"code": "verified-code", "state": "server-stored-state"})
        assert "sso_error" in replay.headers["location"]
        assert await pool.fetchval("SELECT COUNT(*) FROM sso_browser_sessions") == 1


@pytest.mark.parametrize("failure", ["unverified", "revoked_binding", "stale_epoch", "stale_revision", "no_server_challenge"])
async def test_untrusted_or_stale_callback_cannot_create_operation_event(browser, monkeypatch, failure):
    if failure == "unverified":
        browser["oidc"].valid = False
    elif failure == "revoked_binding":
        await browser["pool"].execute("DELETE FROM external_identities WHERE user_id=$1 AND issuer=$2", browser["target"], TARGET_ISSUER)
    elif failure == "stale_epoch":
        monkeypatch.setattr(settings, "sso_session_epoch", uuid.uuid4())
    elif failure == "stale_revision":
        monkeypatch.setattr(runtime_identity, "source_revision", lambda: "b"*40)
    else:
        browser["oidc"].state_challenge = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="https://akb.example") as client:
        response = await client.get("/api/v1/auth/keycloak/callback", params={"code": "verified-code", "state": "server-stored-state",
            "login_challenge_id": browser["challenge"]["challenge_id"]})
        assert response.status_code == 303
    assert await browser["pool"].fetchval("SELECT COUNT(*) FROM sso_callback_login_events") == 0
    assert await browser["pool"].fetchval("SELECT consumed_at FROM sso_operation_login_challenges") is None


async def test_postcheck_hashes_and_service_challenge_authority(browser):
    status = await ops.status(browser["op_id"], credential=browser["grant"]["credential"])
    expected = ops._digest([str(browser["target"])])
    assert status["phase"] == "prepared" and status["existing_identity_ids_hash"] == expected
    assert status["prepared_identity_ids_hash"] == expected
    path = f"/api/v1/admin/sso/identity-operations/{browser['op_id']}/login-challenges"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="https://akb.example") as client:
        pat = {"Authorization": "Bearer " + browser["raw_pat"]}
        assert (await client.post(path, headers=pat, json={"akb_user_id": str(browser["target"])})).status_code == 401
        bounded = {"Authorization": "AKB-Operation " + browser["grant"]["credential"]}
        assert (await client.post(path, headers=bounded, json={"akb_user_id": str(browser["human"])})).status_code == 403
        body = {"akb_user_id": str(browser["target"]), "request_id": str(uuid.uuid4())}
        issued = await client.post(path, headers=bounded, json=body)
        repeated = await client.post(path, headers=bounded, json=body)
        assert repeated.json() == issued.json()
        assert await browser["pool"].fetchval("SELECT COUNT(*) FROM sso_operation_login_challenges") == 2
        assert issued.status_code == 200 and issued.headers["cache-control"] == "no-store"
        assert browser["raw_challenge"] not in repr(dict(await browser["pool"].fetchrow("SELECT * FROM sso_operation_login_challenges LIMIT 1")))


async def test_login_challenge_id_travels_only_in_bound_server_oidc_state(browser, monkeypatch):
    from app.services import keycloak_oidc
    async def pool():
        return browser["pool"]
    monkeypatch.setattr(keycloak_oidc, "get_pool", pool)
    oidc = keycloak_oidc.KeycloakOIDC()
    challenge_id = browser["challenge"]["challenge_id"]
    issued = await oidc.begin_browser_login("/", provider_alias="workforce", login_challenge_id=challenge_id)
    state = parse_qs(urlsplit(issued.location).query)["state"][0]
    assert challenge_id not in issued.location and browser["raw_challenge"] not in issued.location
    assert await oidc.consume_browser_state(state, "incorrect-browser-binding"*2) is None
    transient = await oidc.consume_browser_state(state, issued.browser_binding)
    assert transient["login_challenge_id"] == challenge_id
    assert "browser_binding_hash" not in transient
    assert await oidc.consume_browser_state(state, issued.browser_binding) is None
