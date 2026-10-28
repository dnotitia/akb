"""Fresh human authorization, immutable intent and exact owned compensation."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import uuid

import httpx
import pytest

from app.exceptions import AKBError
from app.services import admin_auth_service
from app.sso import identity_operations as ops, operation_attempts as attempts, callback_evidence as evidence
from tests.test_sso_identity_operations_postgres import request
from tests.test_sso_management_targets_postgres import (  # noqa: F401
    lane, preparation, target_grant, application, configure_payload,
)
from tests.test_sso_callback_evidence_postgres import browser  # noqa: F401

pytestmark = pytest.mark.asyncio


async def fresh(parent_id, pool, human, **overrides):
    row = await pool.fetchrow("SELECT * FROM sso_identity_operations WHERE operation_id=$1", parent_id)
    return await attempts.reauthorize(parent_id, operation_id=uuid.uuid4(), expected_intent_sha256=attempts.intent_hash(row),
                                      ttl_seconds=600, granted_by=human, **overrides)


async def expire(pool, operation_id):
    await pool.execute("""UPDATE sso_identity_operations SET created_at=clock_timestamp()-INTERVAL '20 minutes',
        expires_at=clock_timestamp()-INTERVAL '10 minutes' WHERE operation_id=$1""", operation_id)


async def test_expired_source_attempt_child_rollback_only_owned_binding_preserves_external_changes(lane):  # noqa: F811
    pool, parent_id = lane["pool"], lane["spec"]["operation_id"]
    first = await ops.grant(lane["spec"], granted_by=lane["human"])
    await ops.execute(parent_id, first["credential"], request("apply"))
    owned = await ops.status(parent_id)
    assert owned["owned_binding_count"] == 1
    assert owned["provider_state"] == "configured_disabled" and owned["provider_fingerprint"]
    original_source = owned["source_tuple"]
    # Independent governance changes must not be undone by compensation.
    await pool.execute("DELETE FROM tokens WHERE id=$1", lane["target_tid"])
    await pool.execute("UPDATE vault_access SET role='reader' WHERE vault_id=$1 AND user_id=$2", lane["vid"], lane["target"])
    await expire(pool, parent_id)
    child = await fresh(parent_id, pool, lane["human"])
    assert child["authority_scope"] == "source-recovery" and child["authority_attempt"] == 2
    assert child["parent_operation_id"] == str(parent_id)
    with pytest.raises(AKBError) as retired:
        await ops.execute(parent_id, first["credential"], request("rollback"))
    assert retired.value.code == "sso_operation_retired"
    child_id = uuid.UUID(child["operation_id"])
    rolled = await ops.execute(child_id, child["credential"], request("rollback"))
    assert rolled["binding_changed"] and rolled["state"] == "ready_to_link"
    status = await ops.status(child_id)
    assert status["source_tuple"] == original_source and status["owned_binding_count"] == 0
    assert status["rollback_results"] == [rolled]
    assert status["prepared_identity_ids_hash"] is None and status["existing_identity_ids_hash"]
    assert await pool.fetchval("SELECT COUNT(*) FROM external_identities WHERE user_id=$1", lane["target"]) == 1
    assert await pool.fetchval("SELECT EXISTS(SELECT 1 FROM tokens WHERE id=$1)", lane["target_tid"]) is False
    assert await pool.fetchval("SELECT role FROM vault_access WHERE vault_id=$1 AND user_id=$2", lane["vid"], lane["target"]) == "reader"
    assert await pool.fetchval("SELECT owner_id FROM vaults WHERE id=$1", lane["vid"]) == lane["target"]
    await ops.retire(child_id, credential=child["credential"])
    with pytest.raises(AKBError) as closed:
        await fresh(parent_id, pool, lane["human"])
    assert closed.value.code == "sso_operation_lineage_closed"


async def test_source_child_provider_revision_and_monotonic_supersession(preparation):  # noqa: F811
    payload = configure_payload()
    source_spec = dict(preparation["spec"])
    source_grant = await ops.grant(source_spec, granted_by=preparation["human"])
    root = await target_grant(preparation, payload=payload)
    with pytest.raises(AKBError) as source_cross:
        await ops.execute(source_spec["operation_id"], root["credential"], request("apply"))
    assert source_cross.value.code == "sso_operation_credential_invalid"
    with pytest.raises(AKBError) as target_cross:
        await ops.execute(preparation["spec"]["operation_id"], source_grant["credential"], request("configure", payload=payload))
    assert target_cross.value.code == "sso_operation_credential_invalid"
    parent_id = preparation["spec"]["operation_id"]
    child = await fresh(parent_id, preparation["pool"], preparation["human"])
    child_id = uuid.UUID(child["operation_id"])
    await ops.execute(child_id, child["credential"], request("configure", payload=payload))
    second = await fresh(parent_id, preparation["pool"], preparation["human"])
    assert second["authority_attempt"] == 3 and second["intent_sha256"] == child["intent_sha256"]
    assert second["provider_revision"] == 2
    with pytest.raises(AKBError) as old:
        await ops.execute(child_id, child["credential"], request("configure", payload=payload))
    assert old.value.code == "sso_operation_retired"
    with pytest.raises(AKBError):
        await ops.execute(parent_id, root["credential"], request("configure", payload=payload))
    await ops.execute(uuid.UUID(second["operation_id"]), second["credential"], request("configure", payload=payload))
    assert (await ops.status(parent_id))["provider_revision"] == 3


async def test_target_reauthorization_is_evidence_only_and_new_callback_uses_child(browser):  # noqa: F811
    parent_id = browser["op_id"]
    old_source = (await ops.status(parent_id))["source_tuple"]
    await expire(browser["pool"], parent_id)
    child = await fresh(parent_id, browser["pool"], browser["human"])
    child_id = uuid.UUID(child["operation_id"])
    status = await ops.status(child_id, credential=child["credential"])
    assert child["authority_scope"] == "target-evidence" and status["verbs"] == ["login-evidence"]
    assert status["source_tuple"] == old_source
    for verb in ("apply", "rollback", "enable", "configure"):
        with pytest.raises(AKBError):
            await ops.execute(child_id, child["credential"], request(verb, payload={} if verb in ops.PROVIDER_VERBS else None))
    with pytest.raises(AKBError):
        await evidence.browser_challenge(browser["raw_challenge"], "workforce")
    challenge = await evidence.issue_challenge(child_id, child["credential"], browser["target"], uuid.uuid4())
    browser["oidc"].state_challenge = challenge["challenge_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="https://akb.example") as client:
        response = await client.get("/api/v1/auth/keycloak/callback", params={"code": "verified-code", "state": "server-stored-state"})
        assert response.status_code == 303 and response.headers["location"] == "/", browser["diagnostics"]
    event = await browser["pool"].fetchrow("SELECT * FROM sso_callback_login_events")
    assert event["operation_id"] == child_id and event["challenge_id"] == uuid.UUID(challenge["challenge_id"])
    assert await browser["pool"].fetchval("SELECT retired_at IS NOT NULL FROM sso_identity_operations WHERE operation_id=$1", parent_id)


async def test_fresh_grant_still_rejects_stale_runtime_wrong_intent_drift_and_revocation(preparation, monkeypatch):  # noqa: F811
    await target_grant(preparation, payload=configure_payload())
    parent_id, pool = preparation["spec"]["operation_id"], preparation["pool"]
    row = await pool.fetchrow("SELECT * FROM sso_identity_operations WHERE operation_id=$1", parent_id)
    with pytest.raises(AKBError) as wrong:
        await attempts.reauthorize(parent_id, operation_id=uuid.uuid4(), expected_intent_sha256="0"*64, ttl_seconds=600, granted_by=preparation["human"])
    assert wrong.value.code == "sso_operation_intent_conflict"
    from app.config import settings
    monkeypatch.setattr(settings, "auth_runtime_generation", 3)
    with pytest.raises(AKBError) as stale:
        await fresh(parent_id, pool, preparation["human"])
    assert stale.value.code == "sso_operation_runtime_conflict"
    monkeypatch.setattr(settings, "auth_runtime_generation", 1)
    await pool.execute("UPDATE sso_provider_ownership SET emergency_disabled=true WHERE target_id=$1", preparation["target_id"])
    with pytest.raises(AKBError) as emergency:
        await fresh(parent_id, pool, preparation["human"])
    assert emergency.value.code == "sso_provider_emergency_disabled"
    await pool.execute("UPDATE sso_provider_ownership SET emergency_disabled=false WHERE target_id=$1", preparation["target_id"])
    original = preparation["target_control"].providers[0]
    preparation["target_control"].providers[0] = replace(original, client_id="raw-drift")
    with pytest.raises(AKBError) as drift:
        await fresh(parent_id, pool, preparation["human"])
    assert drift.value.code == "sso_provider_fingerprint_conflict"
    preparation["target_control"].providers[0] = original
    await pool.execute("DELETE FROM tokens WHERE id=$1", preparation["tid"])
    with pytest.raises(AKBError) as revoked:
        await fresh(parent_id, pool, preparation["human"])
    assert revoked.value.code == "sso_operation_actor_retired"
    assert await pool.fetchval("SELECT COUNT(*) FROM sso_identity_operations WHERE parent_operation_id=$1", parent_id) == 0
    assert attempts.intent_hash(row) == (await ops.status(parent_id))["intent_sha256"]


async def test_http_human_csrf_exact_intent_and_original_actor_metadata_only(lane):  # noqa: F811
    parent_id = lane["spec"]["operation_id"]
    await ops.grant(lane["spec"], granted_by=lane["human"])
    body = {"operation_id": str(uuid.uuid4()), "expected_intent_sha256": (await ops.status(parent_id))["intent_sha256"], "ttl_seconds": 600}
    path = f"/api/v1/admin/sso/identity-operations/{parent_id}"
    identity = await admin_auth_service.resolve_prebound_sso_product_admin({"iss": "https://auth.akb.example.com/realms/akb", "sub": "admin-subject"})
    issued = await admin_auth_service.create_sso_admin_browser_session(identity,
        {"iss": "https://auth.akb.example.com/realms/akb", "sub": "admin-subject", "sid": "fresh-human",
         "exp": int((datetime.now(timezone.utc)+timedelta(minutes=10)).timestamp())})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="https://akb.example") as client:
        pat = {"Authorization": "Bearer " + lane["raw_pat"]}
        assert (await client.post(path+"/authority-grants", json=body, headers=pat)).status_code == 401
        client.cookies.set("__Host-akb_admin_session", issued.token)
        client.cookies.set("__Host-akb_admin_csrf", issued.csrf_token)
        assert (await client.post(path+"/authority-grants", json=body)).status_code == 401
        csrf = {"X-AKB-Admin-CSRF": issued.csrf_token}
        assert (await client.post(path+"/authority-grants", json={**body, "verbs": ["configure"]}, headers=csrf)).status_code == 422
        granted = await client.post(path+"/authority-grants", json=body, headers=csrf)
        assert granted.status_code == 200, granted.text
        assert granted.headers["cache-control"] == "no-store"
        client.cookies.clear()
        historical = await client.get(path, headers=pat)
        assert historical.status_code == 200 and historical.json()["retired_at"]
        assert "credential" not in historical.json()
        assert (await client.get(path+"/execute", headers=pat)).status_code == 405
        assert (await client.post(path+"/retire", headers=pat)).status_code == 401


async def test_concurrent_fresh_human_attempts_leave_one_live_authority(lane):  # noqa: F811
    import asyncio
    parent_id = lane["spec"]["operation_id"]
    await ops.grant(lane["spec"], granted_by=lane["human"])
    issued = await asyncio.gather(fresh(parent_id, lane["pool"], lane["human"]), fresh(parent_id, lane["pool"], lane["human"]))
    assert {item["authority_attempt"] for item in issued} == {2, 3}
    from app.db.postgres import _load_migration
    async with lane["pool"].acquire() as conn:
        migration = _load_migration("125_sso_operation_attempts.py")
        await migration.migrate(conn=conn)
        await migration.migrate(conn=conn)
    assert await lane["pool"].fetchval("SELECT lineage_closed_at FROM sso_identity_operations WHERE operation_id=$1", parent_id) is None
    previous, current = sorted(issued, key=lambda item: item["authority_attempt"])
    with pytest.raises(AKBError) as fenced:
        await ops.execute(uuid.UUID(previous["operation_id"]), previous["credential"], request("preflight"))
    assert fenced.value.code == "sso_operation_retired"
    confirmed = await ops.execute(uuid.UUID(current["operation_id"]), current["credential"], request("preflight"))
    assert confirmed["state"] == "ready_to_link"
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_identity_operations WHERE retired_at IS NULL") == 1


async def test_child_rollback_never_deletes_external_recreated_binding(lane):  # noqa: F811
    parent_id, pool = lane["spec"]["operation_id"], lane["pool"]
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    await ops.execute(parent_id, grant["credential"], request("apply"))
    await pool.execute("DELETE FROM external_identities WHERE user_id=$1 AND issuer=$2", lane["target"], "https://auth.akb.example.com/realms/akb")
    replacement = await pool.fetchval("""INSERT INTO external_identities(user_id,issuer,subject)
        VALUES($1,'https://auth.akb.example.com/realms/akb','broker-subject') RETURNING id""", lane["target"])
    child = await fresh(parent_id, pool, lane["human"])
    with pytest.raises(AKBError) as refused:
        await ops.execute(uuid.UUID(child["operation_id"]), child["credential"], request("rollback"))
    assert refused.value.code == "sso_operation_binding_not_owned"
    assert await pool.fetchval("SELECT EXISTS(SELECT 1 FROM external_identities WHERE id=$1)", replacement)
    assert (await ops.status(uuid.UUID(child["operation_id"])))["owned_binding_count"] == 0


async def test_mixed_cohort_status_selects_only_owned_indices_for_compensation(lane):  # noqa: F811
    pool = lane["pool"]
    existing_id = await pool.fetchval("""INSERT INTO external_identities(user_id,issuer,subject)
        VALUES($1,'https://auth.akb.example.com/realms/akb','broker-subject') RETURNING id""", lane["target"])
    added_user = uuid.uuid4()
    await pool.execute("""INSERT INTO users(id,username,email,password_hash,account_kind,auth_provider)
        VALUES($1,'mixed-added','mixed-added@example.test','!fixture!','human','keycloak')""", added_user)
    await pool.execute("""INSERT INTO external_identities(user_id,issuer,subject)
        VALUES($1,'https://accounts.example.com/realms/workforce','upstream-added')""", added_user)
    lane["spec"]["cohort"].append({"existing_user_id": str(added_user),
                                  "upstream_subject": "upstream-added", "broker_subject": "broker-added"})
    parent_id = lane["spec"]["operation_id"]
    grant = await ops.grant(lane["spec"], granted_by=lane["human"])
    preserved = await ops.execute(parent_id, grant["credential"], request("apply", item_index=0))
    assert preserved["binding_changed"] is False
    await ops.execute(parent_id, grant["credential"], request("apply", item_index=1))
    child = await fresh(parent_id, pool, lane["human"])
    child_id = uuid.UUID(child["operation_id"])
    status = await ops.status(child_id, credential=child["credential"])
    assert status["owned_binding_count"] == 1
    assert status["owned_binding_item_indices"] == [1]
    for index in status["owned_binding_item_indices"]:
        result = await ops.execute(child_id, child["credential"], request("rollback", item_index=index))
        assert result["binding_changed"] is True
    status = await ops.status(child_id, credential=child["credential"])
    assert status["owned_binding_count"] == 0 and status["owned_binding_item_indices"] == []
    assert await pool.fetchval("SELECT user_id FROM external_identities WHERE id=$1", existing_id) == lane["target"]
    assert await pool.fetchval("SELECT COUNT(*) FROM external_identities WHERE user_id=$1", added_user) == 1


async def test_latest_whole_cohort_hashes_survive_multiple_expired_attempts(preparation):  # noqa: F811
    pool = preparation["pool"]
    original_cohort = preparation["spec"]["cohort"]
    root = await target_grant(preparation, verbs=["apply", "rollback", "enable", "disable"], cohort=original_cohort)
    root_id = preparation["spec"]["operation_id"]
    original = await pool.fetchrow("SELECT * FROM sso_identity_operations WHERE operation_id=$1", root_id)
    original_intent = attempts.intent_hash(original)
    assert original["prepared_identity_ids_hash"] is None
    # The root expires before preparation; only the second attempt applies it.
    await expire(pool, root_id)
    second = await fresh(root_id, pool, preparation["human"])
    second_id = uuid.UUID(second["operation_id"])
    applied = await ops.execute(second_id, second["credential"], request("apply"))
    assert applied["binding_changed"] and applied["state"] == "linked"
    prepared = await ops.status(second_id)
    hashes = {key: prepared[key] for key in ("existing_identity_ids_hash", "prepared_identity_ids_hash")}
    assert all(hashes.values())
    # A second expiry must inherit the last actual whole-cohort postcheck,
    # rather than the root's never-prepared initial values.
    await expire(pool, second_id)
    third = await fresh(root_id, pool, preparation["human"])
    third_id = uuid.UUID(third["operation_id"])
    status = await ops.status(third_id)
    assert {key: status[key] for key in hashes} == hashes
    assert status["phase"] == "prepared" and third["authority_attempt"] == 3
    enabled = await ops.execute(third_id, third["credential"], request("enable", payload={}))
    assert enabled["provider"]["enabled"] is True
    # The immutable root is a retired journal authority, never reactivated by
    # postchecks/revisions. Old credentials and original intent stay fenced.
    for operation_id, grant in ((root_id, root), (second_id, second)):
        with pytest.raises(AKBError) as retired:
            await ops.execute(operation_id, grant["credential"], request("apply"))
        assert retired.value.code == "sso_operation_retired"
    after = await pool.fetchrow("SELECT * FROM sso_identity_operations WHERE operation_id=$1", root_id)
    assert after["phase"] == "retired" and after["retired_at"] is not None
    assert attempts.intent_hash(after) == original_intent == third["intent_sha256"]
    assert all(after[field] == original[field] for field in attempts.INTENT_FIELDS)
    # A later real rollback clears the lineage's prepared attestation, so a
    # fourth attempt cannot borrow the earlier prepared cohort hash.
    await ops.execute(third_id, third["credential"], request("disable", payload={}))
    await ops.execute(third_id, third["credential"], request("rollback"))
    await expire(pool, third_id)
    fourth = await fresh(root_id, pool, preparation["human"])
    fourth_status = await ops.status(uuid.UUID(fourth["operation_id"]))
    assert fourth["authority_attempt"] == 4 and fourth["intent_sha256"] == original_intent
    assert fourth_status["existing_identity_ids_hash"] == hashes["existing_identity_ids_hash"]
    assert fourth_status["prepared_identity_ids_hash"] is None
    assert fourth_status["phase"] == "granted"
    assert (await ops.status(root_id))["phase"] == "retired"
    assert await pool.fetchval("SELECT user_id FROM tokens WHERE id=$1", preparation["target_tid"]) == preparation["target"]
    assert await pool.fetchval("SELECT role FROM vault_access WHERE vault_id=$1 AND user_id=$2", preparation["vid"], preparation["target"]) == "writer"
