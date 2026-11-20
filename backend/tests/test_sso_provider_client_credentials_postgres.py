"""Client-credential changes and secret-revision evidence against real PostgreSQL.

Only the Keycloak adapter is substituted; its exact representation contract is
covered by the adapter unit tests and the real two-Keycloak broker-chain
fixture. Routes, authority, the ownership guard, the ownership row, events and
audit run as in production.
"""

from __future__ import annotations

from dataclasses import replace
import json
import logging
import uuid

import pytest

from app.api.routes import admin_sso, admin_sso_operations
from app.config import settings
from app.services import admin_auth_service, auth_service
from app.sso import identity_operations as ops, management_targets
from app.sso.keycloak_admin import ProviderControlError
from app.sso.models import ProviderMutationReadback
from tests.test_admin_auth_postgres import _SSO_SESSION_EPOCH, _fresh_database
from tests.test_admin_sso_routes_unit import Control, _provider
from tests.test_sso_identity_operations_postgres import (
    NEW,
    OLD,
    review_http_client,
    review_human_cookies,
)


pytestmark = pytest.mark.asyncio
_PATH = "/api/v1/admin/sso/providers/workforce/client-credentials"
_SECRET = "rotated-fixture-secret-must-never-be-stored"  # pragma: allowlist secret


class StatefulControl(Control):
    """A provider that remembers writes, so readbacks and fingerprints move."""

    def __init__(self) -> None:
        super().__init__()
        self.providers = [_provider(state="enabled")]
        self.stored_secret = "initial-fixture-secret"  # pragma: allowlist secret
        self.drift_after_credentials_write = False

    async def configure(self, spec):
        before = self.providers[0]
        self.stored_secret = spec.client_secret or self.stored_secret
        after = replace(_provider(), client_id=spec.client_id)
        self.providers = [after]
        return ProviderMutationReadback(before=before, after=after)

    async def set_enabled(self, alias, *, enabled):
        before = self.providers[0]
        after = replace(before, enabled=enabled, state="enabled" if enabled else "configured_disabled")
        self.providers = [after]
        self.toggles.append((alias, enabled))
        return ProviderMutationReadback(before=before, after=after)

    async def set_client_credentials(self, alias, *, client_id, client_secret, precondition=None):
        current = self.providers[0]
        if precondition is not None:
            precondition(current)
        self.credential_writes.append((alias, client_id, client_secret))
        self.stored_secret = client_secret
        after = replace(current, client_id=client_id)
        if self.drift_after_credentials_write:
            # Keycloak accepted the write but reads back something else.
            self.providers = [replace(after, display_name="Unexpected SSO")]
            raise ProviderControlError("keycloak_provider_readback_failed")
        self.providers = [after]
        return ProviderMutationReadback(before=current, after=after)


@pytest.fixture
async def standalone(monkeypatch):
    async with _fresh_database() as pool:
        from app.db.postgres import _load_migration

        for name in ("082_pending_admissions.py", "121_sso_identity_operations.py"):
            migration = _load_migration(name)
            assert migration is not None
            async with pool.acquire() as conn:
                await migration.migrate(conn=conn)

        async def get_test_pool():
            return pool

        for module in (ops, auth_service, admin_auth_service, admin_sso_operations):
            monkeypatch.setattr(module, "get_pool", get_test_pool)
        monkeypatch.setattr(settings, "auth_mode", "sso")
        monkeypatch.setattr(settings, "keycloak_enabled", True)
        monkeypatch.setattr(settings, "auth_runtime_generation", 1)
        monkeypatch.setattr(settings, "sso_session_epoch", _SSO_SESSION_EPOCH)
        monkeypatch.setattr(settings, "keycloak_server_url", "https://auth.akb.example.com")
        monkeypatch.setattr(settings, "keycloak_realm", "akb")
        monkeypatch.setattr(settings, "public_base_url", "https://akb.example")
        control = StatefulControl()
        monkeypatch.setattr(ops, "get_keycloak_provider_control", lambda: control)
        monkeypatch.setattr(management_targets, "get_keycloak_provider_control", lambda: control)
        monkeypatch.setattr(admin_sso, "get_keycloak_provider_control", lambda: control)
        audit: list[dict[str, object]] = []
        monkeypatch.setattr(admin_sso.audit_log, "record", lambda **value: audit.append(value))
        human, machine, tid = (uuid.uuid4() for _ in range(3))
        await pool.executemany("""INSERT INTO users(id,username,email,password_hash,is_admin,account_kind,auth_provider)
            VALUES($1,$2,$2||'@example.test','!fixture!',TRUE,$3,'keycloak')""",
            [(human, "admin", "human"), (machine, "machine", "service")])
        await pool.execute("INSERT INTO external_identities(user_id,issuer,subject) VALUES($1,$2,'admin-subject')", human, NEW)
        raw, digest, prefix = auth_service.generate_pat("service")
        await pool.execute("""INSERT INTO tokens(id,user_id,name,token_hash,token_prefix,key_class,scopes)
            VALUES($1,$2,'machine',$3,$4,'service',ARRAY['read','write','admin'])""", tid, machine, digest, prefix)
        yield {"pool": pool, "control": control, "human": human, "audit": audit,
               "pat": {"Authorization": f"Bearer {raw}"}}


def _body(fingerprint: str, **changes: object) -> dict[str, object]:
    body: dict[str, object] = {"client_id": "akb-broker-next", "client_secret": _SECRET,
                               "expected_fingerprint": fingerprint, "secret_revision": "secret-v2"}  # pragma: allowlist secret
    body.update(changes)
    return {key: value for key, value in body.items() if value is not None}


async def _ownership(client, lane) -> dict[str, object]:
    response = await client.get("/api/v1/admin/sso/providers/workforce/ownership", headers=lane["pat"])
    assert response.status_code == 200, response.text
    return response.json()


async def _row(lane):
    return await lane["pool"].fetchrow("SELECT * FROM sso_provider_ownership WHERE alias='workforce'")


async def test_enabled_unowned_provider_credentials_change_and_record_evidence_without_disabling(standalone, caplog):
    lane, control = standalone, standalone["control"]
    caplog.set_level(logging.DEBUG)
    texts: list[str] = []
    async with review_http_client() as client:
        capabilities = await client.get("/api/v1/admin/sso/operation-capabilities", headers=lane["pat"])
        assert "sso.provider-client-credentials.v1" in capabilities.json()["capabilities"]
        before = await _ownership(client, lane)
        assert (before["owner_id"], before["revision"], before["provider_secret_revision"]) == (None, 0, None)
        assert await _row(lane) is None

        changed = await client.put(_PATH, headers=lane["pat"], json=_body(before["fingerprint"]))
        texts.append(changed.text)
        assert changed.status_code == 200, changed.text
        assert changed.json()["provider"]["state"] == "enabled"
        assert changed.json()["provider"]["client_id"] == "akb-broker-next"
        assert control.toggles == []
        after = await _ownership(client, lane)
        texts.append(repr(after))
        assert after["owner_id"] is None and after["revision"] == 1
        assert after["fingerprint"] == ops.provider_fingerprint(control.providers[0]) != before["fingerprint"]
        assert not after["drifted"] and not after["emergency_disabled"]
        assert after["provider_secret_revision"] == "secret-v2"  # pragma: allowlist secret

        # The same pair again changes nothing in the provider and records the
        # label again: that is how a caller restores cleared evidence.
        provider_before_repeat = control.providers[0]
        repeated = await client.put(_PATH, headers=lane["pat"],
                                    json=_body(after["fingerprint"], secret_revision="secret-v2-restored"))
        texts.append(repeated.text)
        assert repeated.status_code == 200
        assert control.providers[0] == provider_before_repeat
        restored = await _ownership(client, lane)
        assert (restored["revision"], restored["fingerprint"]) == (2, after["fingerprint"])
        assert restored["provider_secret_revision"] == "secret-v2-restored"  # pragma: allowlist secret

        # A call without a label clears the stored one.
        unlabeled = await client.put(_PATH, headers=lane["pat"],
                                     json=_body(after["fingerprint"], secret_revision=None))
        texts.append(unlabeled.text)
        assert unlabeled.status_code == 200
        assert (await _ownership(client, lane))["provider_secret_revision"] is None

    events = await lane["pool"].fetch("SELECT kind,payload FROM events WHERE kind='auth.sso_provider_client_credentials_changed' ORDER BY id")
    assert len(events) == 3
    first = events[0]["payload"]
    first = first if isinstance(first, dict) else json.loads(first)
    assert first["provider_alias"] == "workforce" and first["provider_revision"] == 1
    assert first["before"]["client_id"] == "akb-broker" and first["after"]["client_id"] == "akb-broker-next"
    assert [record["action"] for record in lane["audit"]].count("admin.sso.provider.client_credentials") == 3
    assert lane["audit"][1]["meta"]["after"]["client_id"] == "akb-broker-next"
    assert control.credential_writes == [("workforce", "akb-broker-next", _SECRET)] * 3
    # Responses, audit records, events, every database row the flow touched and
    # every log record are scanned for the secret value.
    scanned = [*texts, repr(lane["audit"]), caplog.text]
    for table in ("events", "sso_provider_ownership"):
        scanned.append(repr(await lane["pool"].fetch(f"SELECT * FROM {table}")))
    assert all(_SECRET not in text for text in scanned)


async def test_browser_administrator_needs_the_csrf_header(standalone):
    lane = standalone
    async with review_http_client() as client:
        fingerprint = (await _ownership(client, lane))["fingerprint"]
        csrf = await review_human_cookies(client)
        refused = await client.put(_PATH, json=_body(fingerprint))
        assert refused.status_code == 401
        assert lane["control"].credential_writes == []
        accepted = await client.put(_PATH, headers=csrf, json=_body(fingerprint))
        assert accepted.status_code == 200, accepted.text
        assert _SECRET not in refused.text + accepted.text


async def test_owned_latched_and_stale_requests_are_refused_before_any_write(standalone):
    lane, control = standalone, standalone["control"]
    async with review_http_client() as client:
        current = await _ownership(client, lane)
        stale = await client.put(_PATH, headers=lane["pat"], json=_body("0" * 64))
        assert (stale.status_code, stale.json()) == (409, {"code": "sso_provider_fingerprint_conflict"})
        assert await _row(lane) is None

        await ops.set_ownership("workforce", owner_id="controller-instance", expected_revision=0,
                                expected_fingerprint=current["fingerprint"], clear_emergency=False, actor_id=lane["human"])
        owned = await client.put(_PATH, headers=lane["pat"], json=_body(current["fingerprint"]))
        assert (owned.status_code, owned.json()) == (403, {"code": "sso_provider_owned_mutation_forbidden"})
        human = await review_human_cookies(client)
        owned_by_human = await client.put(_PATH, headers=human, json=_body(current["fingerprint"]))
        assert owned_by_human.status_code == 403

        # A human emergency disable latches; releasing ownership keeps the latch.
        assert (await client.post("/api/v1/admin/sso/providers/workforce/disable", headers=human)).status_code == 200
        latched = await _ownership(client, lane)
        assert latched["emergency_disabled"]
        await ops.set_ownership("workforce", owner_id=None, expected_revision=latched["revision"],
                                expected_fingerprint=latched["fingerprint"], clear_emergency=False, actor_id=lane["human"])
        released = await _ownership(client, lane)
        assert released["owner_id"] is None and released["emergency_disabled"]
        refused = await client.put(_PATH, headers=lane["pat"], json=_body(released["fingerprint"]))
        assert (refused.status_code, refused.json()) == (409, {"code": "sso_provider_emergency_disabled"})
        unknown = await client.put(_PATH, headers=lane["pat"], json={**_body(released["fingerprint"]), "issuer": OLD})
        assert unknown.status_code == 422 and _SECRET not in unknown.text
        missing = await client.put(_PATH, headers=lane["pat"], json=_body(released["fingerprint"], client_secret=None))
        assert missing.status_code == 422
        assert missing.json()["detail"]["code"] == "provider_client_secret_required"
        non_string = await client.put(_PATH, headers=lane["pat"],
                                      json=_body(released["fingerprint"], client_secret={"value": _SECRET}))
        assert non_string.status_code == 422 and _SECRET not in non_string.text
        assert non_string.json()["detail"]["code"] == "provider_client_secret_invalid"
        control_label = await client.put(_PATH, headers=lane["pat"],
                                         json=_body(released["fingerprint"], secret_revision="bad\nlabel"))
        assert control_label.status_code == 422
        assert (await _ownership(client, lane))["revision"] == released["revision"]
    assert control.credential_writes == []


async def test_injected_readback_mismatch_is_an_error_that_changes_nothing_further(standalone):
    lane, control = standalone, standalone["control"]
    async with review_http_client() as client:
        first = await client.put(_PATH, headers=lane["pat"],
                                 json=_body((await _ownership(client, lane))["fingerprint"]))
        assert first.status_code == 200
        row = dict(await _row(lane))
        control.drift_after_credentials_write = True
        failed = await client.put(_PATH, headers=lane["pat"],
                                  json=_body(row["fingerprint"], client_id="akb-broker-third", secret_revision="secret-v3"))
        assert failed.status_code == 409
        assert failed.json()["detail"]["code"] == "keycloak_provider_readback_failed"
        assert _SECRET not in failed.text
    assert dict(await _row(lane)) == row
    assert [write[1] for write in control.credential_writes] == ["akb-broker-next", "akb-broker-third"]
    assert control.toggles == []
    assert await lane["pool"].fetchval(
        "SELECT COUNT(*) FROM events WHERE kind='auth.sso_provider_client_credentials_changed'") == 1
    assert lane["audit"][-1]["outcome"] == "error"
    assert lane["audit"][-1]["code"] == "keycloak_provider_readback_failed"


async def test_secret_revision_survives_emergency_disable_and_a_cleared_latch(standalone):
    lane = standalone
    async with review_http_client() as client:
        changed = await client.put(_PATH, headers=lane["pat"],
                                   json=_body((await _ownership(client, lane))["fingerprint"], secret_revision="secret-v7"))
        assert changed.status_code == 200
        unowned = await _ownership(client, lane)
        await ops.set_ownership("workforce", owner_id="controller-instance", expected_revision=unowned["revision"],
                                expected_fingerprint=unowned["fingerprint"], clear_emergency=False, actor_id=lane["human"])
        assert (await _ownership(client, lane))["provider_secret_revision"] == "secret-v7"  # pragma: allowlist secret
        human = await review_human_cookies(client)
        assert (await client.post("/api/v1/admin/sso/providers/workforce/disable", headers=human)).status_code == 200
        latched = await _ownership(client, lane)
        assert latched["emergency_disabled"] and latched["provider_secret_revision"] == "secret-v7"  # pragma: allowlist secret
        await ops.set_ownership("workforce", owner_id="controller-instance", expected_revision=latched["revision"],
                                expected_fingerprint=latched["fingerprint"], clear_emergency=True, actor_id=lane["human"])
        cleared = await _ownership(client, lane)
        assert not cleared["emergency_disabled"]
        assert cleared["provider_secret_revision"] == "secret-v7"  # pragma: allowlist secret


async def test_secret_revision_survives_ordinary_toggles_and_configure_clears_it(standalone):
    lane = standalone
    async with review_http_client() as client:
        changed = await client.put(_PATH, headers=lane["pat"],
                                   json=_body((await _ownership(client, lane))["fingerprint"], secret_revision="secret-v8"))
        assert changed.status_code == 200
        for verb in ("disable", "enable", "disable"):
            toggled = await client.post(f"/api/v1/admin/sso/providers/workforce/{verb}", headers=lane["pat"])
            assert toggled.status_code == 200, toggled.text
            observed = await _ownership(client, lane)
            assert not observed["drifted"] and observed["provider_secret_revision"] == "secret-v8"  # pragma: allowlist secret
        configured = await client.put("/api/v1/admin/sso/providers/workforce", headers=lane["pat"],
            json={"provider_type": "keycloak-oidc", "display_name": "Company SSO", "issuer": OLD,
                  "discovery_url": OLD + "/.well-known/openid-configuration", "client_id": "akb-broker-next"})
        assert configured.status_code == 200, configured.text
        after_configure = await _ownership(client, lane)
        assert not after_configure["drifted"]
        assert after_configure["provider_secret_revision"] is None


async def test_client_credentials_pin_target_and_use_resolved_control_transport(standalone, monkeypatch):
    import httpx
    from tests.test_sso_keycloak_admin_unit import KeycloakFixture, _control, _spec

    class WithRealm(KeycloakFixture):
        def handler(self, request):
            if request.method == "GET" and request.url.path == "/auth/admin/realms/akb":
                self.requests.append((request.method, request.url.path))
                return httpx.Response(200, json={"id": "fixture-realm-id", "realm": "akb", "enabled": True})
            return super().handler(request)

    lane = standalone
    selected_fixture, wrong_fixture = WithRealm(), WithRealm()
    selected, wrong = _control(selected_fixture), _control(wrong_fixture)
    await selected.configure(_spec())
    selected_fixture.requests.clear()
    monkeypatch.setattr(ops, "get_keycloak_provider_control", lambda: selected)
    monkeypatch.setattr(management_targets, "get_keycloak_provider_control", lambda: selected)
    # The route's unrelated global getter points at a different control. The
    # selected target's transport must be the only one used for this effect.
    monkeypatch.setattr(admin_sso, "get_keycloak_provider_control", lambda: wrong)
    async with review_http_client() as client:
        before = await _ownership(client, lane)
        original = management_targets.resolve
        async def switched(conn, target_id=None):
            target = await original(conn, target_id)
            return replace(target, target_id=uuid.uuid4()) if target_id is None else target
        monkeypatch.setattr(management_targets, "resolve", switched)
        refused = await client.put(_PATH, headers=lane["pat"], json=_body(before["fingerprint"], expected_target_id=before["target_id"]))
        assert (refused.status_code, refused.json()["code"]) == (409, "sso_management_target_conflict")
        assert not any(method == "PUT" for method, _ in selected_fixture.requests)
        assert not wrong_fixture.requests and await _row(lane) is None
        monkeypatch.setattr(management_targets, "resolve", original)
        accepted = await client.put(_PATH, headers=lane["pat"], json=_body(before["fingerprint"], expected_target_id=before["target_id"]))
        assert accepted.status_code == 200, accepted.text
        assert sum(method == "PUT" for method, _ in selected_fixture.requests) == 1
        assert not wrong_fixture.requests and _SECRET not in accepted.text


async def test_client_credentials_refuse_different_realm_before_write(standalone, monkeypatch):
    lane, control = standalone, standalone["control"]
    actual_realm = uuid.uuid4()

    async def read_realm():
        return {"realm_id": str(actual_realm), "realm_name": "akb", "issuer": "https://auth.akb.example.com/realms/akb"}

    monkeypatch.setattr(control, "read_realm_identity", read_realm)
    async with review_http_client() as client:
        before = await _ownership(client, lane)
        request = _body(before["fingerprint"], expected_target_id=before["target_id"], expected_realm_id=str(uuid.uuid4()))
        refused = await client.put(_PATH, headers=lane["pat"], json=request)
        assert (refused.status_code, refused.json()["code"]) == (409, "sso_management_realm_conflict")
        assert control.credential_writes == [] and await _row(lane) is None
        request["expected_realm_id"] = str(actual_realm)
        accepted = await client.put(_PATH, headers=lane["pat"], json=request)
        assert accepted.status_code == 200 and len(control.credential_writes) == 1
