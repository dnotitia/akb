"""Absent-only provider creation, with PostgreSQL authority and HTTP routing."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import auth_service
from app.sso import identity_operations as ops
from app.sso.keycloak_admin import ProviderControlError
from app.sso.models import ProviderMutationReadback
from app.sso.runtime_identity import BASE_CAPABILITIES
from tests.test_admin_sso_routes_unit import _provider
from tests.test_sso_identity_operations_postgres import OLD, review_http_client, review_human_cookies
from tests.test_sso_provider_client_credentials_postgres import standalone as standalone_fixture


standalone = standalone_fixture
pytestmark = pytest.mark.asyncio
_PATH = "/api/v1/admin/sso/providers/workforce/initialize"
_SECRET = "initial-fixture-secret-never-returned"  # pragma: allowlist secret


@pytest.fixture
async def empty(standalone, monkeypatch):
    lane = standalone
    control = lane["control"]
    control.providers = []
    lane["writes"] = []

    async def create(spec, *, require_absent=False):
        assert require_absent is True
        if control.providers:
            raise ProviderControlError("provider_alias_conflict")
        lane["writes"].append(spec.alias)
        control.configured_secret = spec.client_secret
        after = replace(_provider(), client_id=spec.client_id, display_name=spec.display_name)
        control.providers = [after]
        return ProviderMutationReadback(before=None, after=after)

    monkeypatch.setattr(control, "configure", create)
    return lane


def body(**changes):
    value = {"provider_type": "keycloak-oidc", "display_name": "Company SSO", "issuer": OLD,
             "discovery_url": OLD + "/.well-known/openid-configuration", "client_id": "akb-broker-next",
             "client_secret": _SECRET, "expected_fingerprint": ops.provider_fingerprint(None, alias="workforce"),
             "secret_revision": "secret-revision-v1"}  # pragma: allowlist secret
    value.update(changes)
    return value


async def ownership(client, lane):
    result = await client.get("/api/v1/admin/sso/providers/workforce/ownership", headers=lane["pat"])
    assert result.status_code == 200
    return result.json()


async def test_initialize_creates_once_and_records_redacted_atomic_evidence(empty, caplog):
    lane = empty
    caplog.set_level(logging.DEBUG)
    async with review_http_client() as client:
        caps = (await client.get("/api/v1/admin/sso/operation-capabilities", headers=lane["pat"])).json()
        for name in ("sso.provider-initialize.v1", "sso.operation-request-service-key.v1", "sso.provider-target-cas.v1"):
            assert name in caps["capabilities"] and name in BASE_CAPABILITIES
        before = await ownership(client, lane)
        assert before["revision"] == 0 and before["fingerprint"] == body()["expected_fingerprint"]
        result = await client.put(_PATH, headers=lane["pat"], json=body())
        assert result.status_code == 200, result.text
        assert result.json()["provider"]["state"] == "configured_disabled"
        after = await ownership(client, lane)
        assert (after["revision"], after["owner_id"], after["provider_secret_revision"]) == (
            1, None, "secret-revision-v1"
        )
        assert after["fingerprint"] == ops.provider_fingerprint(lane["control"].providers[0])
        assert not after["drifted"] and not after["emergency_disabled"]
        retry = await client.put(_PATH, headers=lane["pat"], json=body())
        assert (retry.status_code, retry.json()["code"]) == (409, "sso_provider_fingerprint_conflict")
        assert lane["writes"] == ["workforce"] and lane["control"].toggles == []
        events = await lane["pool"].fetch("SELECT payload FROM events WHERE kind='auth.sso_provider_initialized'")
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert payload["provider_revision"] == 1 and payload["before"] is None
        assert payload["after"]["state"] == "configured_disabled"
        scanned = [result.text, retry.text, repr(lane["audit"]), caplog.text]
        for table in ("events", "sso_provider_ownership"):
            scanned.append(repr(await lane["pool"].fetch(f"SELECT * FROM {table}")))
        assert all(_SECRET not in text for text in scanned)


async def test_stale_and_existing_provider_refused_before_effect(empty):
    lane = empty
    async with review_http_client() as client:
        stale = await client.put(_PATH, headers=lane["pat"], json=body(expected_fingerprint="0" * 64))
        assert (stale.status_code, stale.json()["code"]) == (409, "sso_provider_fingerprint_conflict")
        lane["control"].providers = [_provider()]
        existing = await client.put(_PATH, headers=lane["pat"], json=body())
        assert (existing.status_code, existing.json()["code"]) == (409, "sso_provider_fingerprint_conflict")
        assert lane["writes"] == []
        assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_provider_ownership") == 0


@pytest.mark.parametrize("condition", ["owner", "latch"])
async def test_owner_and_emergency_latch_refuse_initialization(empty, condition):
    lane = empty
    current = await ops.ownership("workforce")
    await ops.set_ownership("workforce", owner_id="owner" if condition == "owner" else None,
                            expected_revision=0, expected_fingerprint=current["fingerprint"],
                            clear_emergency=False, actor_id=lane["human"])
    if condition == "latch":
        await lane["pool"].execute("UPDATE sso_provider_ownership SET emergency_disabled=TRUE")
    async with review_http_client() as client:
        response = await client.put(_PATH, headers=lane["pat"], json=body())
    expected = "sso_provider_owned_mutation_forbidden" if condition == "owner" else "sso_provider_emergency_disabled"
    assert response.status_code == (403 if condition == "owner" else 409)
    assert response.json()["code"] == expected
    assert lane["writes"] == []


async def test_unowned_absent_existing_row_advances_revision(empty):
    lane = empty
    current = await ops.ownership("workforce")
    await ops.set_ownership("workforce", owner_id=None, expected_revision=0,
                            expected_fingerprint=current["fingerprint"], clear_emergency=False, actor_id=lane["human"])
    async with review_http_client() as client:
        response = await client.put(_PATH, headers=lane["pat"], json=body())
        assert response.status_code == 200, response.text
        after = await ownership(client, lane)
        assert after["revision"] == 2 and after["provider_secret_revision"] == "secret-revision-v1"  # pragma: allowlist secret


async def test_concurrent_initializations_only_one_writes(empty, monkeypatch):
    lane = empty
    started, release = asyncio.Event(), asyncio.Event()
    original = lane["control"].configure

    async def blocked(spec, *, require_absent=False):
        started.set()
        await release.wait()
        return await original(spec, require_absent=require_absent)

    monkeypatch.setattr(lane["control"], "configure", blocked)
    async with review_http_client() as client:
        first = asyncio.create_task(client.put(_PATH, headers=lane["pat"], json=body()))
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(client.put(_PATH, headers=lane["pat"], json=body()))
        await asyncio.sleep(0.1)
        assert not second.done()
        release.set()
        responses = await asyncio.gather(first, second)
        assert sorted(response.status_code for response in responses) == [200, 409]
    assert lane["writes"] == ["workforce"]
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM events WHERE kind='auth.sso_provider_initialized'") == 1


async def test_db_failure_after_provider_create_refuses_retry(empty, monkeypatch):
    lane = empty
    original = ops.emit_event
    monkeypatch.setattr(ops, "emit_event", AsyncMock(side_effect=RuntimeError("simulated audit failure")))
    async with review_http_client() as client:
        with pytest.raises(RuntimeError, match="simulated audit failure"):
            await client.put(_PATH, headers=lane["pat"], json=body())
        assert len(lane["control"].providers) == 1
        assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_provider_ownership") == 0
        monkeypatch.setattr(ops, "emit_event", original)
        retry = await client.put(_PATH, headers=lane["pat"], json=body())
        assert (retry.status_code, retry.json()["code"]) == (409, "sso_provider_fingerprint_conflict")
    assert lane["writes"] == ["workforce"]


async def test_validation_csrf_and_dedicated_key_are_denied_without_secret_echo(empty):
    lane = empty
    async with review_http_client() as client:
        for invalid in ({"expected_fingerprint": "bad"}, {"secret_revision": ""},
                        {"client_secret": ["invalid"]}, {"unexpected": _SECRET}):
            response = await client.put(_PATH, headers=lane["pat"], json=body(**invalid))
            assert response.status_code == 422
            assert _SECRET not in response.text
        for field in ("client_secret", "expected_fingerprint", "secret_revision"):
            missing = body()
            del missing[field]
            response = await client.put(_PATH, headers=lane["pat"], json=missing)
            assert response.status_code == 422 and _SECRET not in response.text
        csrf = await review_human_cookies(client)
        assert (await client.put(_PATH, json=body())).status_code == 401
        assert (await client.put(_PATH, headers=csrf, json=body())).status_code == 200

    # A fresh dedicated requester remains unable to enter SSO control routes.
    uid, tid = uuid.uuid4(), uuid.uuid4()
    await lane["pool"].execute("""INSERT INTO users(id,username,email,password_hash,is_admin,account_kind,auth_provider)
        VALUES($1,'requester','requester@example.test','!fixture!',FALSE,'service','service')""", uid)
    raw, digest, prefix = auth_service.generate_pat("service")
    await lane["pool"].execute("""INSERT INTO tokens(id,user_id,name,token_hash,token_prefix,key_class,scopes)
        VALUES($1,$2,'requester',$3,$4,'service',ARRAY['sso_operation_requests'])""", tid, uid, digest, prefix)
    async with review_http_client() as client:
        denied = await client.put(_PATH, headers={"Authorization": "Bearer " + raw}, json=body())
        assert denied.status_code == 403 and _SECRET not in denied.text
    assert lane["writes"] == ["workforce"]


async def test_real_adapter_and_mock_keycloak_transport_with_postgres(standalone, monkeypatch, caplog):
    """Exercise route, transaction and native HTTP adapter together."""
    import httpx

    from app.api.routes import admin_sso
    from app.sso import management_targets
    from tests.test_sso_keycloak_admin_unit import KeycloakFixture, _control

    class WithRealm(KeycloakFixture):
        def handler(self, request):
            if request.method == "GET" and request.url.path == "/auth/admin/realms/akb":
                self.requests.append((request.method, request.url.path))
                return httpx.Response(200, json={"id": "fixture-realm-id", "realm": "akb", "enabled": True})
            return super().handler(request)

    lane = standalone
    fixture = WithRealm()
    control = _control(fixture)
    monkeypatch.setattr(ops, "get_keycloak_provider_control", lambda: control)
    monkeypatch.setattr(management_targets, "get_keycloak_provider_control", lambda: control)
    monkeypatch.setattr(admin_sso, "get_keycloak_provider_control", lambda: control)
    caplog.set_level(logging.DEBUG)
    async with review_http_client() as client:
        before = await ownership(client, lane)
        assert before["fingerprint"] == body()["expected_fingerprint"]
        created = await client.put(_PATH, headers=lane["pat"], json=body())
        assert created.status_code == 200, created.text
        after = await ownership(client, lane)
        assert after["revision"] == 1 and after["provider_secret_revision"] == "secret-revision-v1"  # pragma: allowlist secret
        assert after["fingerprint"] == ops.provider_fingerprint(
            (await control.list_providers(force_refresh=True))[0]
        )
        assert fixture.providers["workforce"]["enabled"] is False
        collection = "/auth/admin/realms/akb/identity-provider/instances"
        assert sum(method == "POST" and path == collection for method, path in fixture.requests) == 1
        assert not any(method == "PUT" for method, _ in fixture.requests)
        event = await lane["pool"].fetchval("SELECT payload FROM events WHERE kind='auth.sso_provider_initialized'")
        assert json.loads(event)["before"] is None
        assert _SECRET not in created.text + repr(lane["audit"]) + event + caplog.text


async def test_initialize_refuses_active_target_switch_before_effect(empty, monkeypatch):
    from app.sso import management_targets

    lane = empty
    async with review_http_client() as client:
        before = await ownership(client, lane)
        original = management_targets.resolve
        async def switched(conn, target_id=None):
            target = await original(conn, target_id)
            return replace(target, target_id=uuid.uuid4()) if target_id is None else target
        monkeypatch.setattr(management_targets, "resolve", switched)
        rejected = await client.put(_PATH, headers=lane["pat"], json=body(expected_target_id=before["target_id"]))
        assert (rejected.status_code, rejected.json()["code"]) == (409, "sso_management_target_conflict")
        assert _SECRET not in rejected.text and lane["writes"] == []
        assert await lane["pool"].fetchval("SELECT count(*) FROM sso_provider_ownership") == 0
        monkeypatch.setattr(management_targets, "resolve", original)
        accepted = await client.put(_PATH, headers=lane["pat"], json=body(expected_target_id=before["target_id"]))
        assert accepted.status_code == 200 and lane["writes"] == ["workforce"]


async def test_initialize_refuses_different_realm_before_write(empty, monkeypatch):
    lane = empty
    actual_realm = uuid.uuid4()

    async def read_realm():
        return {"realm_id": str(actual_realm), "realm_name": "akb", "issuer": "https://auth.akb.example.com/realms/akb"}

    monkeypatch.setattr(lane["control"], "read_realm_identity", read_realm)
    async with review_http_client() as client:
        before = await ownership(client, lane)
        request = body(expected_target_id=before["target_id"], expected_realm_id=str(uuid.uuid4()))
        refused = await client.put(_PATH, headers=lane["pat"], json=request)
        assert (refused.status_code, refused.json()["code"]) == (409, "sso_management_realm_conflict")
        assert lane["writes"] == [] and await lane["pool"].fetchval("SELECT count(*) FROM sso_provider_ownership") == 0
        request["expected_realm_id"] = str(actual_realm)
        accepted = await client.put(_PATH, headers=lane["pat"], json=request)
        assert accepted.status_code == 200 and lane["writes"] == ["workforce"]
