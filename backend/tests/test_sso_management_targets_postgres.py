"""Management-only target authority through real PostgreSQL and HTTP.

Keycloak transport/readback is substituted; no claim of live broker behavior.
"""
from dataclasses import replace
import json
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest

from app.api.routes import admin_sso, auth
from app.config import settings
from app.exceptions import AKBError
from app.sso import identity_operations as ops, management_targets, runtime_identity
from app.sso.models import ProviderMutationReadback
from tests.test_admin_sso_routes_unit import Control
from tests.test_sso_identity_operations_postgres import lane, request, run, assert_continuity  # noqa: F401

pytestmark = pytest.mark.asyncio
TARGET_ISSUER = "https://workspace.example.test/realms/workspace"


class TargetControl(Control):
    async def read_realm_identity(self):
        return {"realm_id": "workspace-realm-uuid", "realm_name": "workspace", "issuer": TARGET_ISSUER}

    async def configure(self, spec):
        self.configured_secret = spec.client_secret
        before = self.providers[0]
        self.providers = [replace(before, state="configured_disabled", enabled=False, client_id=spec.client_id)]
        return ProviderMutationReadback(before=before, after=self.providers[0])

    async def set_enabled(self, alias, *, enabled):
        before = self.providers[0]
        self.providers = [replace(before, state="enabled" if enabled else "configured_disabled", enabled=enabled)]
        return ProviderMutationReadback(before=before, after=self.providers[0])

    async def verify_identity_prelink(self, alias, **kwargs):
        return replace(await super().verify_identity_prelink(alias, **kwargs), broker_issuer=TARGET_ISSUER)


@pytest.fixture
async def preparation(lane, monkeypatch, tmp_path):  # noqa: F811
    target_id, epoch = uuid.uuid4(), uuid.uuid4()
    config = {"schema_version": 1, "targets": [{"target_id": str(target_id), "realm_id": "workspace-realm-uuid",
        "realm_name": "workspace", "issuer": TARGET_ISSUER, "internal_url": "http://private-keycloak.test:8080",
        "management_client_id": "narrow-fixture-manager", "management_client_secret": "fixture-only-not-a-live-secret",
        "session_epoch": str(epoch)}]}
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setattr(settings, "sso_management_targets_file", str(path))
    control = TargetControl()
    configs = []
    def target_control(config):
        configs.append(config)
        return control
    monkeypatch.setattr(management_targets, "KeycloakProviderControl", target_control)
    management_targets._controls.clear()
    return {**lane, "target_id": target_id, "target_epoch": epoch, "target_control": control,
            "target_file": path, "target_config": config, "source": runtime_identity.source_tuple(), "control_configs": configs}


async def target_grant(preparation, *, verbs=None, cohort=None, payload=None):
    view = await ops.ownership("workforce", target_id=preparation["target_id"])
    if view["owner_id"] is None:
        view = await ops.set_ownership("workforce", target_id=preparation["target_id"], owner_id="target-owner",
            expected_revision=view["revision"], expected_fingerprint=view["fingerprint"], clear_emergency=False,
            actor_id=preparation["human"])
    spec = {**preparation["spec"], "operation_id": uuid.uuid4(), "target_id": preparation["target_id"],
            "expected_source": preparation["source"], "expected_target_realm_id": "workspace-realm-uuid",
            "target_session_epoch": preparation["target_epoch"], "expected_target_runtime_generation": 2,
            "expected_target_source_revision": runtime_identity.source_revision(), "provider_secret_revision": "secret-v1",
            "owner_id": "target-owner", "expected_provider_revision": view["revision"],
            "verbs": verbs or ["configure"], "cohort": cohort or [],
            "provider_payload_sha256": ({"configure": ops._digest(payload)} if payload is not None else
                                        {verb: ops._digest({}) for verb in (verbs or []) if verb in ops.PROVIDER_VERBS})}
    preparation["spec"] = spec
    return await ops.grant(spec, granted_by=preparation["human"])


def application():
    app = FastAPI()
    app.include_router(admin_sso.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    @app.exception_handler(AKBError)
    async def error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)
    return app


def configure_payload():
    return {"provider_type": "keycloak-oidc", "display_name": "Workspace SSO",
            "issuer": "https://accounts.example.com/realms/workforce",
            "discovery_url": "https://accounts.example.com/realms/workforce/.well-known/openid-configuration",
            "client_id": "target-broker", "client_secret": "fixture-provider-secret"}


async def switch_target(preparation, monkeypatch):
    monkeypatch.setattr(settings, "keycloak_server_url", "https://workspace.example.test")
    monkeypatch.setattr(settings, "keycloak_realm", "workspace")
    monkeypatch.setattr(settings, "auth_runtime_generation", 2)
    monkeypatch.setattr(settings, "sso_session_epoch", preparation["target_epoch"])
    await preparation["pool"].execute("UPDATE auth_runtime_state SET runtime_generation=2,sso_session_epoch=$1 WHERE singleton", preparation["target_epoch"])


async def test_target_configure_keeps_active_verifier_and_same_alias_ownership_separate(preparation):
    before = runtime_identity.source_tuple()
    payload = configure_payload()
    grant = await target_grant(preparation, payload=payload)
    body = request("configure", payload=payload)
    result = await run(preparation, grant, body)
    assert result["provider"]["client_id"] == "target-broker"
    assert preparation["control"].configured_secret is None
    assert preparation["control_configs"][0].realm == "workspace"
    assert preparation["control_configs"][0].management_client_id == "narrow-fixture-manager"
    assert preparation["control_configs"][0].internal_base_url == "http://private-keycloak.test:8080"
    assert runtime_identity.source_tuple() == before
    active = await ops.ownership("workforce")
    target = await ops.ownership("workforce", target_id=preparation["target_id"])
    assert active["target_id"] != target["target_id"] and active["owner_id"] == "controller-instance"
    assert target["owner_id"] == "target-owner" and target["provider_secret_revision"] == "secret-v1"
    # Rebuilding the management client is a restart, not a new authority.
    management_targets._controls.clear()
    assert await run(preparation, grant, body) == result
    await assert_continuity(preparation)
    # Omitted secret preserves verified version; raw visible drift invalidates it.
    without_secret = {key: value for key, value in payload.items() if key != "client_secret"}
    second = await target_grant(preparation, payload=without_secret)
    await run(preparation, second, request("configure", payload=without_secret))
    assert (await ops.ownership("workforce", target_id=preparation["target_id"]))["provider_secret_revision"] == "secret-v1"
    preparation["target_control"].providers[0] = replace(preparation["target_control"].providers[0], client_id="raw-change")
    drift = await ops.ownership("workforce", target_id=preparation["target_id"])
    assert drift["drifted"] and drift["provider_secret_revision"] is None


@pytest.mark.parametrize("change", ["removed", "changed", "realm"])
async def test_removed_or_changed_installed_target_never_falls_back(preparation, change, monkeypatch):
    payload = configure_payload()
    grant = await target_grant(preparation, payload=payload)
    if change == "removed":
        preparation["target_config"]["targets"] = []
    elif change == "changed":
        preparation["target_config"]["targets"][0]["internal_url"] = "http://different-private.test:8080"
    else:
        async def recreated():
            return {"realm_id": "recreated-realm-uuid", "realm_name": "workspace", "issuer": TARGET_ISSUER}
        monkeypatch.setattr(preparation["target_control"], "read_realm_identity", recreated)
    preparation["target_file"].write_text(json.dumps(preparation["target_config"]))
    with pytest.raises(AKBError) as denied:
        await run(preparation, grant, request("configure", payload=payload))
    assert denied.value.code in {"sso_management_target_unavailable", "sso_management_target_changed"}
    assert preparation["target_control"].configured_secret is None
    assert preparation["control"].configured_secret is None


async def test_source_and_target_epoch_boundaries_and_credential_crossuse(preparation, monkeypatch):
    payload = configure_payload()
    grant = await target_grant(preparation, payload=payload)
    bad = {**preparation["spec"], "operation_id": uuid.uuid4(), "target_session_epoch": uuid.uuid4()}
    with pytest.raises(AKBError, match="rejected"):
        await ops.grant(bad, granted_by=preparation["human"])
    with pytest.raises(AKBError) as cross:
        await ops.execute(uuid.uuid4(), grant["credential"], request("configure", payload=payload))
    assert cross.value.status_code == 401
    monkeypatch.setattr(settings, "auth_runtime_generation", 3)
    with pytest.raises(AKBError) as stale:
        await run(preparation, grant, request("configure", payload=payload))
    assert stale.value.code == "sso_operation_runtime_conflict"
    monkeypatch.setattr(settings, "auth_runtime_generation", 1)
    await switch_target(preparation, monkeypatch)
    # Same installed UUID after switch; alias ownership does not follow epoch.
    assert (await ops.ownership("workforce"))["target_id"] == str(preparation["target_id"])
    assert (await ops.status(preparation["spec"]["operation_id"], credential=grant["credential"]))["target_epoch"] == str(preparation["target_epoch"])
    with pytest.raises(AKBError) as mutation:
        await run(preparation, grant, request("configure", payload=payload))
    assert mutation.value.code == "sso_operation_runtime_conflict"
    assert (await ops.retire(preparation["spec"]["operation_id"], credential=grant["credential"]))["phase"] == "retired"


async def test_target_http_metadata_pat_cannot_grant_or_adopt_and_unknown_target(preparation):
    pat = {"Authorization": "Bearer " + preparation["raw_pat"]}
    path = f"/api/v1/admin/sso/targets/{preparation['target_id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="https://akb.example") as client:
        inventory = await client.get("/api/v1/admin/sso/targets", headers=pat)
        assert inventory.status_code == 200
        assert len(inventory.json()["targets"]) == 2
        assert "fixture-only-not-a-live-secret" not in inventory.text and "internal_url" not in inventory.text
        catalog = await client.get(path+"/providers", headers=pat)
        assert catalog.status_code == 200 and catalog.json()["schema_version"] == 1
        assert catalog.json()["auth_mode"] == "sso" and "supported_provider_types" in catalog.json()
        assert (await client.put(path+"/providers/workforce/ownership", headers=pat,
            json={"owner_id": "stolen", "expected_revision": 0, "expected_fingerprint": "a"*64})).status_code == 401
        assert (await client.post("/api/v1/admin/sso/identity-operations", headers=pat,
            json={})).status_code in {401, 422}
        unknown = await client.get(f"/api/v1/admin/sso/targets/{uuid.uuid4()}/providers", headers=pat)
        assert unknown.status_code == 409


async def test_public_capabilities_exact_source_unknown_and_no_default_preparation(lane, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(settings, "sso_management_targets_file", "")
    monkeypatch.setattr(runtime_identity, "SOURCE_REVISION_FILE", tmp_path / "absent")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="https://akb.example") as client:
        response = await client.get("/api/v1/auth/identity-capabilities")
        assert response.status_code == 200
        assert response.json() == {"schema_version": 1, **runtime_identity.source_tuple(), "capabilities": runtime_identity.BASE_CAPABILITIES}
        assert response.json()["source_revision"] is None
        assert (await client.get("/api/v1/admin/sso/targets")).status_code == 401
        pat = {"Authorization": "Bearer " + lane["raw_pat"]}
        assert len((await client.get("/api/v1/admin/sso/targets", headers=pat)).json()["targets"]) == 1
        runtime_identity.SOURCE_REVISION_FILE.write_text("backend-v0.14.2")
        assert (await client.get("/api/v1/auth/identity-capabilities")).json()["source_revision"] is None
        runtime_identity.SOURCE_REVISION_FILE.write_text("a"*40)
        assert (await client.get("/api/v1/auth/identity-capabilities")).json()["source_revision"] == "a"*40


async def test_local_source_provider_preparation_never_installs_second_verifier(preparation, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "sso_session_epoch", None)
    preparation["source"] = runtime_identity.source_tuple()
    preparation["spec"].pop("sso_session_epoch")
    await preparation["pool"].execute("UPDATE auth_runtime_state SET auth_mode='local',sso_session_epoch=NULL WHERE singleton")
    grant = await target_grant(preparation, payload=configure_payload())
    await run(preparation, grant, request("configure", payload=configure_payload()))
    assert settings.require_auth_mode() == "local" and runtime_identity.source_tuple()["issuer"] is None
    assert settings.sso_session_epoch is None


async def test_target_migration_upgrades_alias_namespace_idempotently(lane):  # noqa: F811
    from app.db.postgres import _load_migration
    pool = lane["pool"]
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE sso_provider_ownership DROP COLUMN target_id CASCADE")
        await conn.execute("ALTER TABLE sso_provider_ownership ADD PRIMARY KEY(alias)")
        await conn.execute("ALTER TABLE sso_operation_installation DROP COLUMN legacy_target_id, DROP COLUMN legacy_issuer")
        migration = _load_migration("122_sso_management_targets.py")
        await migration.migrate(conn=conn)
        target_id = await conn.fetchval("SELECT target_id FROM sso_provider_ownership WHERE alias='workforce'")
        await migration.migrate(conn=conn)
        assert await conn.fetchval("SELECT target_id FROM sso_provider_ownership WHERE alias='workforce'") == target_id
        assert await conn.fetchval("SELECT legacy_target_id FROM sso_operation_installation") == target_id
        assert await conn.fetchval("SELECT owner_id FROM sso_provider_ownership") == "controller-instance"


async def test_local_preparation_target_removal_after_switch_cannot_hide_ownership(preparation, monkeypatch):
    # A local source has no legacy issuer to fence by; the durable installed
    # issuer/UUID pin must still prevent fallback to a fresh active namespace.
    await preparation["pool"].execute("UPDATE sso_operation_installation SET legacy_issuer=NULL")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "sso_session_epoch", None)
    await ops.ownership("workforce", target_id=preparation["target_id"])
    monkeypatch.setattr(settings, "auth_mode", "sso")
    await switch_target(preparation, monkeypatch)
    preparation["target_file"].write_text('{"schema_version":1,"targets":[]}')
    with pytest.raises(AKBError) as blocked:
        await ops.ownership("workforce")
    assert blocked.value.code == "sso_management_target_unavailable"
