"""Atomic operation/cohort authority against isolated real PostgreSQL.

Only the Keycloak readback is substituted. Real Keycloak semantics belong to
the disposable broker-chain fixture, not this database/HTTP contract suite.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest

from app.api.routes import admin_sso, admin_sso_operations
from app.config import settings
from app.exceptions import AKBError
from app.services import admin_auth_service, auth_service
from app.sso import identity_operations as ops, management_targets
from tests.test_admin_auth_postgres import _SSO_SESSION_EPOCH, _fresh_database
from tests.test_admin_sso_routes_unit import Control, _provider


pytestmark = pytest.mark.asyncio
OLD = "https://accounts.example.com/realms/workforce"
NEW = "https://auth.akb.example.com/realms/akb"


@pytest.fixture
async def lane(monkeypatch):
    async with _fresh_database() as pool:
        from app.db.postgres import _load_migration

        admission_migration = _load_migration("082_pending_admissions.py")
        operation_migration = _load_migration("121_sso_identity_operations.py")
        assert admission_migration is not None
        assert operation_migration is not None
        async with pool.acquire() as conn:
            await admission_migration.migrate(conn=conn)
            installation = await conn.fetchval("SELECT installation_id FROM sso_operation_installation")
            await operation_migration.migrate(conn=conn)
            await operation_migration.migrate(conn=conn)
            assert await conn.fetchval("SELECT installation_id FROM sso_operation_installation") == installation
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
        control = Control()
        monkeypatch.setattr(ops, "get_keycloak_provider_control", lambda: control)
        monkeypatch.setattr(management_targets, "get_keycloak_provider_control", lambda: control)
        monkeypatch.setattr(admin_sso, "get_keycloak_provider_control", lambda: control)
        human, machine, target, tid, target_tid, vid = (uuid.uuid4() for _ in range(6))
        await pool.executemany("""INSERT INTO users(id,username,email,password_hash,is_admin,account_kind,auth_provider)
            VALUES($1,$2,$2||'@example.test','!fixture!',$3,$4,'keycloak')""", [
                (human, "admin", True, "human"), (machine, "machine", True, "service"),
                (target, "target", False, "human")])
        await pool.executemany("INSERT INTO external_identities(user_id,issuer,subject) VALUES($1,$2,$3)",
                              [(human, NEW, "admin-subject"), (target, OLD, "upstream-subject")])
        raw, digest, prefix = auth_service.generate_pat("service")
        await pool.execute("""INSERT INTO tokens(id,user_id,name,token_hash,token_prefix,key_class,scopes)
            VALUES($1,$2,'machine',$3,$4,'service',ARRAY['read','write','admin'])""", tid, machine, digest, prefix)
        await pool.execute("INSERT INTO tokens(id,user_id,name,token_hash,token_prefix) VALUES($1,$2,'target','target-hash','target-prefix')", target_tid, target)
        await pool.execute("INSERT INTO vaults(id,name,owner_id,git_path) VALUES($1,'continuity',$2,'/unused')", vid, target)
        await pool.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'writer')", vid, target)
        owner = await ops.set_ownership("workforce", owner_id="controller-instance", expected_revision=0,
            expected_fingerprint=ops.provider_fingerprint(control.providers[0]), clear_emergency=False, actor_id=human)
        spec = {"operation_id": uuid.uuid4(), "installation_id": uuid.UUID((await ops.capabilities())["installation_id"]),
                "actor_id": machine, "actor_token_id": tid, "provider_alias": "workforce",
                "owner_id": "controller-instance", "expected_provider_revision": owner["revision"],
                "runtime_generation": 1, "sso_session_epoch": _SSO_SESSION_EPOCH, "ttl_seconds": 600,
                "verbs": ["preflight", "apply", "rollback"],
                "cohort": [{"existing_user_id": str(target), "upstream_subject": "upstream-subject", "broker_subject": "broker-subject"}]}
        yield {"pool": pool, "control": control, "human": human, "machine": machine, "target": target,
               "tid": tid, "target_tid": target_tid, "vid": vid, "raw_pat": raw, "spec": spec}


def request(verb="apply", **overrides):
    return {"nonce": uuid.uuid4(), "verb": verb, "item_index": 0,
            "issued_at": datetime.now(timezone.utc) - timedelta(seconds=1), **overrides}


async def run(lane, grant, body):
    return await ops.execute(lane["spec"]["operation_id"], grant["credential"], body)


async def assert_continuity(lane):
    pool = lane["pool"]
    assert await pool.fetchval("SELECT user_id FROM tokens WHERE id=$1", lane["target_tid"]) == lane["target"]
    assert await pool.fetchval("SELECT owner_id FROM vaults WHERE id=$1", lane["vid"]) == lane["target"]
    assert await pool.fetchval("SELECT role FROM vault_access WHERE user_id=$1 AND vault_id=$2", lane["target"], lane["vid"]) == "writer"


async def test_atomic_apply_replay_scope_and_owned_rollback_preserve_account(lane):
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    pool = lane["pool"]
    stored = await pool.fetchrow("SELECT * FROM sso_identity_operations")
    assert grant["credential"] not in repr(dict(stored))
    assert len(stored["credential_hash"]) == 64
    assert stored["expires_at"] - stored["created_at"] == timedelta(seconds=600)
    body = request()
    first = await run(lane, grant, body)
    assert first["state"] == "linked" and first["binding_changed"]
    assert await run(lane, grant, body) == first
    with pytest.raises(AKBError) as conflict:
        await run(lane, grant, {**body, "verb": "rollback"})
    assert conflict.value.code == "sso_operation_nonce_conflict"
    for changed in (request(item_index=1), request("admit")):
        with pytest.raises(AKBError) as denied:
            await run(lane, grant, changed)
        assert denied.value.code == "sso_operation_scope_forbidden"
    replayed_item = await run(lane, grant, request())
    assert not replayed_item["binding_changed"]
    rolled = await run(lane, grant, request("rollback"))
    assert rolled["state"] == "ready_to_link" and rolled["binding_changed"]
    assert await pool.fetchval("SELECT COUNT(*) FROM sso_identity_operation_requests") == 3
    assert await pool.fetchval("SELECT COUNT(*) FROM external_identities WHERE user_id=$1", lane["target"]) == 1
    await assert_continuity(lane)


async def test_enabled_admission_is_explicit_and_does_not_disable_provider(lane):
    lane["control"].providers = [_provider(state="enabled")]
    owner = await ops.set_ownership("workforce", owner_id="controller-instance", expected_revision=1,
        expected_fingerprint=ops.provider_fingerprint(lane["control"].providers[0]), clear_emergency=False, actor_id=lane["human"])
    lane["spec"]["expected_provider_revision"] = owner["revision"]
    lane["spec"]["verbs"] = ["apply", "admit"]
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    with pytest.raises(AKBError) as disabled:
        await run(lane, grant, request("apply"))
    assert disabled.value.code == "identity_migration_provider_must_be_disabled"
    assert (await run(lane, grant, request("admit")))["state"] == "linked"
    assert lane["control"].providers[0].enabled
    assert lane["control"].toggles == []
    await assert_continuity(lane)


async def test_local_and_wrong_issuer_cohorts_cannot_be_granted(lane):
    pool = lane["pool"]
    await pool.execute("UPDATE users SET auth_provider='local' WHERE id=$1", lane["target"])
    with pytest.raises(ops.identity_migration.IdentityMigrationError) as local:
        await ops.grant(lane["spec"], granted_by=lane["human"])
    assert local.value.code == "identity_migration_target_not_external"
    await pool.execute("UPDATE users SET auth_provider='keycloak' WHERE id=$1", lane["target"])
    await pool.execute("UPDATE external_identities SET issuer='https://other.example/realms/akb' WHERE user_id=$1", lane["target"])
    with pytest.raises(ops.identity_migration.IdentityMigrationError) as issuer:
        await ops.grant(lane["spec"], granted_by=lane["human"])
    assert issuer.value.code == "identity_migration_old_binding_missing"
    assert await pool.fetchval("SELECT COUNT(*) FROM sso_identity_operations") == 0
    await assert_continuity(lane)


async def test_retirement_expiry_token_revoke_and_admin_demotion_fail_closed(lane):
    pool = lane["pool"]
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    body = request("preflight")
    await run(lane, grant, body)
    for change, restore in [
        ("UPDATE users SET is_admin=false WHERE id=$1", "UPDATE users SET is_admin=true WHERE id=$1"),
    ]:
        await pool.execute(change, lane["human"])
        with pytest.raises(AKBError) as demoted:
            await run(lane, grant, body)
        assert demoted.value.code == "sso_operation_actor_retired"
        await pool.execute(restore, lane["human"])
    await pool.execute("UPDATE tokens SET expires_at=NOW()-INTERVAL '1 second' WHERE id=$1", lane["tid"])
    with pytest.raises(AKBError) as revoked:
        await run(lane, grant, body)
    assert revoked.value.code == "sso_operation_actor_retired"
    await pool.execute("UPDATE tokens SET expires_at=NULL WHERE id=$1", lane["tid"])
    with pytest.raises(AKBError) as nonce:
        await run(lane, grant, request(issued_at=datetime.now(timezone.utc)-timedelta(seconds=301)))
    assert nonce.value.code == "sso_operation_nonce_expired"
    await ops.retire(lane["spec"]["operation_id"], actor_id=lane["human"])
    with pytest.raises(AKBError) as retired:
        await run(lane, grant, body)
    assert retired.value.code == "sso_operation_retired"
    assert grant["credential"] not in repr(await ops.status(lane["spec"]["operation_id"]))


async def test_revision_drift_emergency_disable_and_postcheck_rollback(lane, monkeypatch):
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    original = lane["control"].providers[0]
    lane["control"].providers[0] = replace(original, client_id="unexpected-client")
    with pytest.raises(AKBError) as drift:
        await run(lane, grant, request())
    assert drift.value.code == "sso_provider_fingerprint_conflict"
    lane["control"].providers[0] = original
    calls = 0
    async def changed_after_write(alias, item, target=None):
        nonlocal calls
        calls += 1
        prelink = await lane["control"].verify_identity_prelink(alias,
            broker_subject=item["broker_subject"], upstream_subject=item["upstream_subject"])
        return replace(prelink, upstream_issuer=OLD+"/changed") if calls == 2 else prelink
    monkeypatch.setattr(ops, "_verify_item", changed_after_write)
    with pytest.raises(AKBError) as postcheck:
        await run(lane, grant, request())
    assert postcheck.value.code == "identity_migration_prelink_changed"
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM external_identities WHERE user_id=$1", lane["target"]) == 1
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_identity_operation_requests") == 0
    actor = await admin_auth_service.resolve_prebound_sso_product_admin({"iss": NEW, "sub": "admin-subject"})
    with pytest.raises(AKBError) as protected:
        async with ops.provider_mutation_guard("workforce", actor, enabled=True):
            pytest.fail("protected enable must not execute")
    assert protected.value.code == "sso_provider_owned_mutation_forbidden"
    async with ops.provider_mutation_guard("workforce", actor, enabled=False) as complete:
        await complete(original)
    assert (await ops.ownership("workforce"))["emergency_disabled"]
    with pytest.raises(AKBError) as revision:
        await run(lane, grant, request())
    assert revision.value.code == "sso_provider_revision_conflict"


async def test_human_http_grant_csrf_and_service_metadata_boundary(lane):
    app = FastAPI()
    app.include_router(admin_sso.router, prefix="/api/v1")
    @app.exception_handler(AKBError)
    async def error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)
    identity = await admin_auth_service.resolve_prebound_sso_product_admin({"iss": NEW, "sub": "admin-subject"})
    issued = await admin_auth_service.create_sso_admin_browser_session(identity,
        {"iss": NEW, "sub": "admin-subject", "sid": "fixture-human-sid",
         "exp": int((datetime.now(timezone.utc)+timedelta(minutes=10)).timestamp())})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example") as client:
        spec = {key: str(value) if isinstance(value, uuid.UUID) else value for key, value in lane["spec"].items()}
        pat = {"Authorization": f"Bearer {lane['raw_pat']}"}
        assert (await client.get("/api/v1/admin/sso/operation-capabilities", headers=pat)).status_code == 200
        assert (await client.get("/api/v1/admin/sso/providers/workforce/ownership", headers=pat)).status_code == 200
        assert (await client.post("/api/v1/admin/sso/identity-operations", headers=pat, json=spec)).status_code == 401
        client.cookies.set("__Host-akb_admin_session", issued.token)
        client.cookies.set("__Host-akb_admin_csrf", issued.csrf_token)
        assert (await client.post("/api/v1/admin/sso/identity-operations", json=spec)).status_code == 401
        response = await client.post("/api/v1/admin/sso/identity-operations", json=spec,
                                     headers={"X-AKB-Admin-CSRF": issued.csrf_token})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = request()
        wire = {key: value.isoformat() if isinstance(value, datetime) else str(value)
                if isinstance(value, uuid.UUID) else value for key, value in body.items()}
        operation_id = spec["operation_id"]
        path = f"/api/v1/admin/sso/identity-operations/{operation_id}/execute"
        assert (await client.post(path, json=wire, headers=pat)).status_code == 401
        applied = await client.post(path, json=wire, headers={"Authorization": "AKB-Operation "+response.json()["credential"]})
        assert applied.status_code == 200 and applied.json()["state"] == "linked"
        invalid = await client.post("/api/v1/admin/sso/identity-operations", json={"cohort": spec["cohort"]},
                                    headers={"X-AKB-Admin-CSRF": issued.csrf_token})
        assert invalid.status_code == 422 and "upstream-subject" not in invalid.text


async def test_provider_payload_grant_serializes_writes_and_keeps_secrets_out_of_journal(lane):
    from app.sso.models import ProviderMutationReadback

    class MutableControl(Control):
        async def configure(self, spec):
            result = await super().configure(spec)
            self.providers = [result.after]
            return result

        async def set_enabled(self, alias, *, enabled):
            before = self.providers[0]
            after = replace(before, enabled=enabled, state="enabled" if enabled else "configured_disabled")
            self.providers = [after]
            self.toggles.append((alias, enabled))
            return ProviderMutationReadback(before=before, after=after)

    control = lane["control"]
    control.__class__ = MutableControl
    payload = {"provider_type": "keycloak-oidc", "display_name": "Company SSO", "issuer": OLD,
               "discovery_url": OLD+"/.well-known/openid-configuration", "client_id": "akb-broker",
               "client_secret": "ephemeral-fixture-provider-secret"}  # pragma: allowlist secret
    lane["spec"].update(verbs=["configure", "enable", "disable"], cohort=[],
        provider_payload_sha256={"configure": ops._digest(payload), "enable": ops._digest({}), "disable": ops._digest({})})
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    with pytest.raises(AKBError) as mismatch:
        await run(lane, grant, request("configure", payload={**payload, "client_id": "another-client"}))
    assert mismatch.value.code == "sso_operation_payload_conflict"
    configured = await run(lane, grant, request("configure", payload=payload))
    assert configured["provider_revision"] == 2
    body = request("enable", payload={})
    enabled = await run(lane, grant, body)
    assert enabled["provider_revision"] == 3 and enabled["provider"]["enabled"]
    assert await run(lane, grant, body) == enabled
    assert control.toggles == [("workforce", True)]
    disabled = await run(lane, grant, request("disable", payload={}))
    assert disabled["provider_revision"] == 4 and not disabled["provider"]["enabled"]
    for table in ("sso_identity_operations", "sso_identity_operation_requests", "events"):
        assert payload["client_secret"] not in repr(await lane["pool"].fetch(f"SELECT * FROM {table}"))
    assert (await ops.ownership("workforce"))["revision"] == 4


async def test_runtime_installation_nonce_expiry_and_foreign_binding_guards(lane, monkeypatch):
    pool = lane["pool"]
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    await pool.execute("INSERT INTO external_identities(user_id,issuer,subject) VALUES($1,$2,'broker-subject')", lane["target"], NEW)
    with pytest.raises(AKBError) as foreign:
        await run(lane, grant, request("rollback"))
    assert foreign.value.code == "sso_operation_binding_not_owned"
    with pytest.raises(AKBError) as forged:
        await ops.execute(lane["spec"]["operation_id"], "akb_sso_operation_wrong", request())
    assert forged.value.code == "sso_operation_credential_invalid"
    await pool.execute("UPDATE sso_operation_installation SET installation_id=$1", uuid.uuid4())
    with pytest.raises(AKBError) as installation:
        await run(lane, grant, request())
    assert installation.value.code == "sso_operation_installation_conflict"
    await pool.execute("UPDATE sso_operation_installation SET installation_id=$1", lane["spec"]["installation_id"])
    monkeypatch.setattr(settings, "auth_runtime_generation", 2)
    await pool.execute("UPDATE auth_runtime_state SET runtime_generation=2")
    with pytest.raises(AKBError) as generation:
        await run(lane, grant, request())
    assert generation.value.code == "sso_operation_runtime_conflict"
    monkeypatch.setattr(settings, "auth_runtime_generation", 1)
    await pool.execute("UPDATE auth_runtime_state SET runtime_generation=1")
    await pool.execute("""UPDATE sso_identity_operations SET created_at=NOW()-INTERVAL '601 seconds',
        expires_at=NOW()-INTERVAL '1 second'""")
    with pytest.raises(AKBError) as expiry:
        await run(lane, grant, request())
    assert expiry.value.code == "sso_operation_retired"
    await assert_continuity(lane)


async def test_service_pat_sql_cannot_mutate_operation_ownership(lane):
    from app.services.user_sql_executor import PermissionDeniedError, UserSqlExecutor
    from app.services.role_sync import RoleSync

    actor = await auth_service.resolve_rest_user_authorization("Bearer "+lane["raw_pat"])
    assert actor is not None and actor.is_admin and actor.account_kind == "service"
    await RoleSync(lane["pool"]).on_user_create(actor.user_id)
    executor = UserSqlExecutor(lane["pool"])
    with pytest.raises(PermissionDeniedError) as denied:
        await executor.execute(user_id=actor.user_id, actor_id=actor.username, is_admin=actor.is_admin,
            sql="UPDATE sso_provider_ownership SET owner_id='bypass' WHERE alias='workforce'")
    assert denied.value.pg_sqlstate == "42501"
    assert (await ops.ownership("workforce"))["owner_id"] == "controller-instance"
    # Full human administration remains the explicit trust boundary.
    result = await executor.execute(user_id=lane["human"], actor_id="admin", is_admin=True,
        sql="SELECT owner_id FROM sso_provider_ownership")
    assert result["items"] == [{"owner_id": "controller-instance"}]
    # The machine retains ordinary granted-vault SQL through RoleSync.
    sync = RoleSync(lane["pool"])
    await sync.on_vault_create(lane["vid"], lane["target"])
    await lane["pool"].execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'writer')", lane["vid"], lane["machine"])
    await sync.on_grant(lane["vid"], lane["machine"], "writer")
    async with lane["pool"].acquire() as conn:
        await conn.execute("CREATE TABLE vt_continuity__allowed (value INTEGER)")
        await sync.grant_table_in_conn(conn, lane["vid"], "vt_continuity__allowed")
    result = await executor.execute(user_id=actor.user_id, actor_id=actor.username, is_admin=True,
        sql="INSERT INTO vt_continuity__allowed VALUES(7)")
    assert result["affected_rows"] == 1


async def test_existing_machine_role_projection_is_a_trusted_admin_surface(lane, monkeypatch):
    from app.api.routes import access
    from app.services import account_service

    async def get_test_pool():
        return lane["pool"]
    monkeypatch.setattr(account_service, "get_pool", get_test_pool)
    app = FastAPI()
    app.include_router(access.router, prefix="/api/v1")
    @app.exception_handler(AKBError)
    async def error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example") as client:
        response = await client.put(f"/api/v1/admin/users/{lane['target']}/role", json={"is_admin": True},
            headers={"Authorization": "Bearer "+lane["raw_pat"]})
        unapproved_mint = await client.post(f"/api/v1/admin/users/{lane['target']}/tokens", json={"name": "unapproved-mint"},
            headers={"Authorization": "Bearer "+lane["raw_pat"]})
    assert response.status_code == 200
    assert unapproved_mint.status_code == 403 and unapproved_mint.json()["code"] == "human_token_issuer_required"
    assert await lane["pool"].fetchval("SELECT is_admin FROM users WHERE id=$1", lane["target"])


async def test_machine_identity_binding_requires_operation_but_exact_projection_survives(lane, monkeypatch):
    from app.api.routes import access
    from app.services import account_service

    async def get_test_pool():
        return lane["pool"]
    monkeypatch.setattr(account_service, "get_pool", get_test_pool)
    app = FastAPI()
    app.include_router(access.router, prefix="/api/v1")
    @app.exception_handler(AKBError)
    async def error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)
    payload = {"issuer": NEW, "subject": "new-machine-subject", "email": "target@example.test",
               "existing_user_id": str(lane["target"])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example") as client:
        headers = {"Authorization": "Bearer "+lane["raw_pat"]}
        denied = await client.post("/api/v1/admin/users/ensure-external-identity", headers=headers, json=payload)
        assert denied.status_code == 403 and denied.json()["code"] == "bounded_identity_operation_required"
        await lane["pool"].execute("INSERT INTO external_identities(user_id,issuer,subject) VALUES($1,$2,$3)",
                                  lane["target"], NEW, "new-machine-subject")
        same = await client.post("/api/v1/admin/users/ensure-external-identity", headers=headers, json=payload)
        assert same.status_code == 200 and same.json()["user_id"] == str(lane["target"])
    await assert_continuity(lane)


def review_http_client():
    app = FastAPI()
    app.include_router(admin_sso.router, prefix="/api/v1")

    @app.exception_handler(AKBError)
    async def error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example")


async def review_human_cookies(client):
    identity = await admin_auth_service.resolve_prebound_sso_product_admin({"iss": NEW, "sub": "admin-subject"})
    issued = await admin_auth_service.create_sso_admin_browser_session(identity,
        {"iss": NEW, "sub": "admin-subject", "sid": "review-fixture-human",
         "exp": int((datetime.now(timezone.utc)+timedelta(minutes=10)).timestamp())})
    client.cookies.set("__Host-akb_admin_session", issued.token)
    client.cookies.set("__Host-akb_admin_csrf", issued.csrf_token)
    return {"X-AKB-Admin-CSRF": issued.csrf_token}


async def test_review_noncanonical_alias_cannot_bypass_owned_provider_guard(lane, monkeypatch):
    from app.sso.providers.keycloak_oidc import validate_alias

    control = lane["control"]
    called = []
    original_configure, original_toggle = control.configure, control.set_enabled
    async def configure(spec):
        # Exercise the production adapter's alias normalization after the guard.
        canonical = validate_alias(spec.alias)
        called.append(("configure", canonical))
        return await original_configure(replace(spec, alias=canonical))
    async def toggle(alias, *, enabled):
        canonical = validate_alias(alias)
        called.append(("enable" if enabled else "disable", canonical))
        return await original_toggle(canonical, enabled=enabled)
    monkeypatch.setattr(control, "configure", configure)
    monkeypatch.setattr(control, "set_enabled", toggle)
    payload = {"provider_type": "keycloak-oidc", "display_name": "Company SSO", "issuer": OLD,
               "discovery_url": OLD+"/.well-known/openid-configuration", "client_id": "akb-broker"}
    async with review_http_client() as client:
        pat = {"Authorization": "Bearer "+lane["raw_pat"]}
        results = [await client.put("/api/v1/admin/sso/providers/%20workforce%20", headers=pat, json=payload)]
        for verb in ("enable", "disable"):
            results.append(await client.post(f"/api/v1/admin/sso/providers/%20workforce%20/{verb}", headers=pat))
        assert [result.status_code for result in results] == [422, 422, 422]
        assert called == []
        assert (await ops.ownership("workforce"))["revision"] == 1
        human = await review_human_cookies(client)
        raw_disable = await client.post("/api/v1/admin/sso/providers/%20workforce%20/disable", headers=human)
        assert raw_disable.status_code == 422
        assert (await ops.ownership("workforce"))["revision"] == 1
        canonical_disable = await client.post("/api/v1/admin/sso/providers/workforce/disable", headers=human)
        assert canonical_disable.status_code == 200
        owner = await ops.ownership("workforce")
        assert owner["revision"] == 2 and owner["emergency_disabled"]
        assert called == [("disable", "workforce")]


async def review_waiting_transaction(pool):
    async with asyncio.timeout(10):
        while True:
            waiting = await pool.fetchrow("""SELECT xact_start,query FROM pg_stat_activity
                WHERE datname=current_database() AND wait_event_type='Lock'
                AND pid<>pg_backend_pid() ORDER BY xact_start LIMIT 1""")
            if waiting:
                return waiting
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("gate", ["operation", "actor_token", "provider_lock"])
async def test_review_expiry_while_waiting_for_authority_locks_cannot_write(lane, gate):
    pool = lane["pool"]
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    body = request()
    pending = None
    if gate == "provider_lock":
        await pool.execute("""WITH deadline AS (SELECT clock_timestamp()+INTERVAL '3 seconds' AS at)
            UPDATE sso_identity_operations SET created_at=deadline.at-INTERVAL '600 seconds',
            expires_at=deadline.at FROM deadline WHERE operation_id=$1""", lane["spec"]["operation_id"])
    try:
        async with pool.acquire() as holder, holder.transaction():
            if gate == "operation":
                await holder.fetchrow("SELECT operation_id FROM sso_identity_operations WHERE operation_id=$1 FOR UPDATE", lane["spec"]["operation_id"])
            elif gate == "actor_token":
                await holder.fetchrow("SELECT id FROM tokens WHERE id=$1 FOR UPDATE", lane["tid"])
            else:
                await ops._alias_lock(holder, "workforce")
            pending = asyncio.create_task(run(lane, grant, body))
            waiting = await review_waiting_transaction(pool)
            deadline = waiting["xact_start"]+timedelta(milliseconds=10)
            if gate == "operation":
                await holder.execute("UPDATE sso_identity_operations SET created_at=$2::timestamptz-INTERVAL '600 seconds',expires_at=$2::timestamptz WHERE operation_id=$1",
                                     lane["spec"]["operation_id"], deadline)
            elif gate == "actor_token":
                await holder.execute("UPDATE tokens SET expires_at=$2 WHERE id=$1", lane["tid"], deadline)
            else:
                deadline = await holder.fetchval("SELECT expires_at FROM sso_identity_operations WHERE operation_id=$1", lane["spec"]["operation_id"])
                assert waiting["xact_start"] < deadline
            async with asyncio.timeout(10):
                while not await holder.fetchval("SELECT clock_timestamp()>$1", deadline):
                    await asyncio.sleep(0.01)
        with pytest.raises(AKBError) as expired:
            await pending
        assert expired.value.code == ("sso_operation_actor_retired" if gate == "actor_token" else "sso_operation_retired")
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert await pool.fetchval("SELECT COUNT(*) FROM sso_identity_operation_requests") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM external_identities WHERE user_id=$1", lane["target"]) == 1
    await assert_continuity(lane)


async def test_review_invalid_owned_provider_can_be_inspected_released_and_reconfigured(lane, monkeypatch):
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    control = lane["control"]
    control.providers[0] = replace(control.providers[0], state="configuration_error", issuer=None)
    with pytest.raises(AKBError) as unavailable:
        await run(lane, grant, request())
    assert unavailable.value.code == "sso_operation_provider_unavailable"
    with pytest.raises(AKBError) as cannot_grant:
        await ops.grant({**lane["spec"], "operation_id": uuid.uuid4()}, granted_by=lane["human"])
    assert cannot_grant.value.code == "sso_operation_provider_unavailable"
    original_configure = control.configure
    async def configure(spec):
        result = await original_configure(spec)
        control.providers = [result.after]
        return result
    monkeypatch.setattr(control, "configure", configure)
    async with review_http_client() as client:
        metadata = await client.get("/api/v1/admin/sso/providers/workforce/ownership",
            headers={"Authorization": "Bearer "+lane["raw_pat"]})
        assert metadata.status_code == 200
        observed = metadata.json()
        assert observed["drifted"] and observed["owner_id"] == "controller-instance"
        human = await review_human_cookies(client)
        released = await client.put("/api/v1/admin/sso/providers/workforce/ownership", headers=human,
            json={"owner_id": None, "expected_revision": observed["revision"],
                  "expected_fingerprint": observed["fingerprint"]})
        assert released.status_code == 200 and released.json()["owner_id"] is None
        configured = await client.put("/api/v1/admin/sso/providers/workforce", headers=human,
            json={"provider_type": "keycloak-oidc", "display_name": "Company SSO", "issuer": OLD,
                  "discovery_url": OLD+"/.well-known/openid-configuration", "client_id": "akb-broker"})
        assert configured.status_code == 200 and configured.json()["provider"]["state"] == "configured_disabled"
    assert not (await ops.ownership("workforce"))["drifted"]
