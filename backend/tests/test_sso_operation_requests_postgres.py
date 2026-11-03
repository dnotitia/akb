"""Operation requests against real PostgreSQL and HTTP.

Only the Keycloak adapter is substituted, as in the other operation suites.
Routes, both credential carriers, the human session and CSRF, the grant checks,
the ownership guard, events and the HPKE seal run as in production.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from app.api.routes import admin_sso, admin_sso_operations
from app.config import settings
from app.services import admin_auth_service, auth_service
from app.sso import identity_operations as ops, management_targets, operation_requests, sealing
from app.sso.models import ProviderMutationReadback
from tests.test_admin_auth_postgres import _SSO_SESSION_EPOCH, _fresh_database
from tests.test_admin_sso_routes_unit import Control
from tests.test_sso_identity_operations_postgres import NEW, OLD, review_http_client, review_human_cookies


pytestmark = pytest.mark.asyncio
BASE = "/api/v1/admin/sso/operation-requests"
OWNER = "controller-instance"
SECRET = "request-fixture-provider-secret-never-stored"  # pragma: allowlist secret
PAYLOAD = {"provider_type": "keycloak-oidc", "display_name": "Company SSO", "issuer": OLD,
           "discovery_url": OLD + "/.well-known/openid-configuration", "client_id": "akb-broker-next",
           "client_secret": SECRET}
SUMMARY = {**{key: value for key, value in PAYLOAD.items() if key != "client_secret"}, "client_secret_supplied": True}


class MutableControl(Control):
    """Keycloak substitute whose readback follows writes, so fingerprints move."""

    async def configure(self, spec):
        before = self.providers[0]
        self.configured_secret = spec.client_secret
        after = replace(before, client_id=spec.client_id, display_name=spec.display_name,
                        state="configured_disabled", enabled=False)
        self.providers = [after]
        return ProviderMutationReadback(before=before, after=after)

    async def set_enabled(self, alias, *, enabled):
        before = self.providers[0]
        after = replace(before, enabled=enabled, state="enabled" if enabled else "configured_disabled")
        self.providers = [after]
        self.toggles.append((alias, enabled))
        return ProviderMutationReadback(before=before, after=after)


async def _service_token(pool, username: str, scopes: list[str]) -> tuple[uuid.UUID, uuid.UUID, dict[str, str]]:
    user_id, token_id = uuid.uuid4(), uuid.uuid4()
    await pool.execute("""INSERT INTO users(id,username,email,password_hash,is_admin,account_kind,auth_provider)
        VALUES($1,$2,$2||'@example.test','!fixture!',TRUE,'service','keycloak')""", user_id, username)
    raw, digest, prefix = auth_service.generate_pat("service")
    await pool.execute("""INSERT INTO tokens(id,user_id,name,token_hash,token_prefix,key_class,scopes)
        VALUES($1,$2,$3,$4,$5,'service',$6::text[])""", token_id, user_id, username, digest, prefix, scopes)
    return user_id, token_id, {"Authorization": f"Bearer {raw}"}


@pytest.fixture
async def lane(monkeypatch):
    async with _fresh_database() as pool:
        from app.db.postgres import _load_migration

        async with pool.acquire() as conn:
            for name in ("082_pending_admissions.py", "121_sso_identity_operations.py",
                         "127_sso_operation_requests.py", "127_sso_operation_requests.py"):
                migration = _load_migration(name)
                assert migration is not None
                await migration.migrate(conn=conn)

        async def get_test_pool():
            return pool

        for module in (ops, auth_service, admin_auth_service, admin_sso_operations, operation_requests):
            monkeypatch.setattr(module, "get_pool", get_test_pool)
        monkeypatch.setattr(settings, "auth_mode", "sso")
        monkeypatch.setattr(settings, "keycloak_enabled", True)
        monkeypatch.setattr(settings, "auth_runtime_generation", 1)
        monkeypatch.setattr(settings, "sso_session_epoch", _SSO_SESSION_EPOCH)
        monkeypatch.setattr(settings, "keycloak_server_url", "https://auth.akb.example.com")
        monkeypatch.setattr(settings, "keycloak_realm", "akb")
        monkeypatch.setattr(settings, "public_base_url", "https://akb.example")
        control = MutableControl()
        monkeypatch.setattr(ops, "get_keycloak_provider_control", lambda: control)
        monkeypatch.setattr(management_targets, "get_keycloak_provider_control", lambda: control)
        monkeypatch.setattr(admin_sso, "get_keycloak_provider_control", lambda: control)
        monkeypatch.setattr(admin_sso.audit_log, "record", lambda **value: None)
        human = uuid.uuid4()
        await pool.execute("""INSERT INTO users(id,username,email,password_hash,is_admin,account_kind,auth_provider)
            VALUES($1,'admin','admin@example.test','!fixture!',TRUE,'human','keycloak')""", human)
        await pool.execute("INSERT INTO external_identities(user_id,issuer,subject) VALUES($1,$2,'admin-subject')", human, NEW)
        machine, tid, pat = await _service_token(pool, "machine", ["read", "write", "admin"])
        _, other_tid, other_pat = await _service_token(pool, "other-machine", ["read", "write", "admin"])
        _, _, reader_pat = await _service_token(pool, "reader-machine", ["read"])
        yield {"pool": pool, "control": control, "human": human, "machine": machine, "tid": tid,
               "pat": pat, "other_pat": other_pat, "other_tid": other_tid, "reader_pat": reader_pat}


def keypair() -> tuple[X25519PrivateKey, str]:
    private = X25519PrivateKey.generate()
    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unseal(sealed: str, private: X25519PrivateKey, info: str) -> str:
    data = base64.urlsafe_b64decode(sealed + "=" * (-len(sealed) % 4))
    return sealing.SUITE.decrypt(data, private, info=info.encode()).decode()


def digest(document) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


async def own(lane, owner_id=OWNER) -> dict:
    current = await ops.ownership("workforce")
    return await ops.set_ownership("workforce", owner_id=owner_id, expected_revision=current["revision"],
                                   expected_fingerprint=current["fingerprint"], clear_emergency=False,
                                   actor_id=lane["human"])


def grant_body(public_key: str, revision: int, **changes) -> dict:
    body = {"kind": "provider_grant", "operation_id": str(uuid.uuid4()), "provider_alias": "workforce",
            "owner_id": OWNER, "expected_provider_revision": revision, "verbs": ["configure", "enable"],
            "provider_payload_sha256": {"configure": ops._digest(PAYLOAD), "enable": ops._digest({})},
            "configure_summary": SUMMARY, "ttl_seconds": 600, "provider_secret_revision": "secret-v2",  # pragma: allowlist secret
            "recipient_public_key": public_key}
    body.update(changes)
    return body


def execute_body(verb: str, payload: dict) -> dict:
    return {"nonce": str(uuid.uuid4()), "verb": verb, "item_index": 0, "payload": payload,
            "issued_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}


async def propose_and_approve(client, lane, csrf, body) -> dict:
    proposed = await client.post(BASE, headers=lane["pat"], json=body)
    assert proposed.status_code == 201, proposed.text
    request = proposed.json()
    approved = await client.post(f"{BASE}/{request['request_id']}/approve", headers=csrf,
                                 json={"expected_request_sha256": request["request_sha256"]})
    assert approved.status_code == 200, approved.text
    return approved.json()


async def events(lane, prefix="auth.sso_operation_request_") -> list[tuple[str, dict]]:
    rows = await lane["pool"].fetch("SELECT kind,payload FROM events WHERE kind LIKE $1 ORDER BY id", prefix + "%")
    return [(row["kind"], row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])) for row in rows]


async def test_claim_is_proposed_by_a_service_key_and_approved_by_the_exact_human_digest(lane):
    async with review_http_client() as client:
        capabilities = await client.get("/api/v1/admin/sso/operation-capabilities", headers=lane["pat"])
        assert "sso.operation-requests.v1" in capabilities.json()["capabilities"]
        before = await ops.ownership("workforce")
        claim = {"kind": "ownership_claim", "provider_alias": "workforce", "owner_id": OWNER,
                 "expected_revision": before["revision"], "expected_fingerprint": before["fingerprint"]}

        # Without a service key: nothing, a human session, a key without admin scope.
        assert (await client.post(BASE, json=claim)).json()["code"] == "sso_operation_request_service_key_required"
        csrf = await review_human_cookies(client)
        as_human = await client.post(BASE, json=claim, headers=csrf)
        assert (as_human.status_code, as_human.json()["code"]) == (403, "sso_operation_request_service_key_required")
        assert (await client.post(BASE, json=claim, headers=lane["reader_pat"])).status_code == 403
        # The requester comes from the calling token; the body cannot name it.
        named = await client.post(BASE, json={**claim, "requested_by": str(lane["human"])}, headers=lane["pat"])
        assert (named.status_code, named.json()["detail"]["code"]) == (422, "sso_operation_request_invalid")
        assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_operation_requests") == 0

        proposed = await client.post(BASE, json=claim, headers=lane["pat"])
        assert proposed.status_code == 201, proposed.text
        request = proposed.json()
        assert request["status"] == "pending" and request["requester_username"] == "machine"
        assert request["request"]["requested_by"] == str(lane["machine"])
        assert request["request"]["requested_token_id"] == str(lane["tid"])
        assert request["request_sha256"] == digest(request["request"])
        assert datetime.fromisoformat(request["expires_at"]) - datetime.fromisoformat(request["created_at"]) == timedelta(seconds=3600)

        listed = await client.get(BASE)
        assert [item["request_id"] for item in listed.json()["requests"]] == [request["request_id"]]
        assert (await client.get(f"{BASE}/{request['request_id']}", headers=lane["pat"])).json() == request
        foreign = await client.get(f"{BASE}/{request['request_id']}", headers=lane["other_pat"])
        assert (foreign.status_code, foreign.json()["code"]) == (403, "sso_operation_request_scope_forbidden")
        assert (await client.get(BASE, headers=lane["other_pat"])).json()["requests"] == []

        path = f"{BASE}/{request['request_id']}/approve"
        decision = {"expected_request_sha256": request["request_sha256"]}
        # A service key, or a human session without the CSRF header, cannot approve.
        assert (await client.post(path, json=decision, headers=lane["pat"])).status_code == 401
        assert (await client.post(path, json=decision)).status_code == 401
        stale = await client.post(path, json={"expected_request_sha256": "0" * 64}, headers=csrf)
        assert (stale.status_code, stale.json()["code"]) == (409, "sso_operation_request_digest_conflict")
        approved = await client.post(path, json=decision, headers=csrf)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved" and approved.json()["decided_by"] == str(lane["human"])
        again = await client.post(path, json=decision, headers=csrf)
        assert (again.status_code, again.json()["code"]) == (409, "sso_operation_request_not_pending")

    row = await lane["pool"].fetchrow("SELECT * FROM sso_provider_ownership WHERE alias='workforce'")
    assert (row["owner_id"], row["revision"], row["updated_by"]) == (OWNER, before["revision"] + 1, lane["human"])
    assert [kind for kind, _ in await events(lane)] == ["auth.sso_operation_request_proposed",
                                                        "auth.sso_operation_request_approved"]
    changed = await events(lane, "auth.sso_provider_ownership_changed")
    assert len(changed) == 1 and changed[0][1]["owner_id"] == OWNER


async def test_a_stale_claim_stays_pending_and_reports_the_conflict(lane):
    async with review_http_client() as client:
        before = await ops.ownership("workforce")
        proposed = (await client.post(BASE, headers=lane["pat"], json={
            "kind": "ownership_claim", "provider_alias": "workforce", "owner_id": OWNER,
            "expected_revision": before["revision"], "expected_fingerprint": before["fingerprint"]})).json()
        lane["control"].providers = [replace(lane["control"].providers[0], display_name="Changed SSO")]
        csrf = await review_human_cookies(client)
        conflict = await client.post(f"{BASE}/{proposed['request_id']}/approve", headers=csrf,
                                     json={"expected_request_sha256": proposed["request_sha256"]})
        assert (conflict.status_code, conflict.json()["code"]) == (409, "sso_provider_fingerprint_conflict")
        assert (await client.get(f"{BASE}/{proposed['request_id']}")).json()["status"] == "pending"
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_provider_ownership") == 0


async def test_an_approved_grant_is_collected_once_sealed_to_the_requesters_key(lane, caplog):
    caplog.set_level(logging.DEBUG)
    owned = await own(lane)
    private, public = keypair()
    human_texts: list[str] = []
    async with review_http_client() as client:
        csrf = await review_human_cookies(client)
        proposed = await client.post(BASE, headers=lane["pat"], json=grant_body(public, owned["revision"]))
        assert proposed.status_code == 201, proposed.text
        request = proposed.json()
        assert request["request"]["grant"]["configure_summary"] == SUMMARY
        assert request["request_sha256"] == digest(request["request"])
        collect = f"{BASE}/{request['request_id']}/credential"
        early = await client.post(collect, headers=lane["pat"])
        assert (early.status_code, early.json()["code"]) == (409, "sso_operation_request_not_approved")

        listed = await client.get(BASE)
        human_texts.append(listed.text)
        approved = await client.post(f"{BASE}/{request['request_id']}/approve", headers=csrf,
                                     json={"expected_request_sha256": request["request_sha256"]})
        human_texts.append(approved.text)
        decided = approved.json()
        assert datetime.fromisoformat(decided["operation_expires_at"]) - datetime.fromisoformat(decided["decided_at"]) == timedelta(seconds=600)
        # Approving a grant records the approval only.
        assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_identity_operations") == 0

        foreign = await client.post(collect, headers=lane["other_pat"])
        assert (foreign.status_code, foreign.json()["code"]) == (403, "sso_operation_request_scope_forbidden")
        as_human = await client.post(collect, headers=csrf)
        assert (as_human.status_code, as_human.json()["code"]) == (403, "sso_operation_request_service_key_required")

        collected = await client.post(collect, headers=lane["pat"])
        assert collected.status_code == 200, collected.text
        assert collected.headers["cache-control"] == "no-store" and collected.headers["pragma"] == "no-cache"
        sealed = collected.json()
        assert sealed["seal"] == {"mode": "base", "suite": sealing.SUITE_NAME,
                                  "info": f"{sealing.INFO_PREFIX}:{request['request_id']}:{request['request_sha256']}"}
        credential = unseal(sealed["sealed_credential"], private, sealed["seal"]["info"])
        assert credential.startswith(ops.PREFIX)
        # Only the request's private key and the request's own binding open it.
        with pytest.raises(InvalidTag):
            unseal(sealed["sealed_credential"], X25519PrivateKey.generate(), sealed["seal"]["info"])
        with pytest.raises(InvalidTag):
            unseal(sealed["sealed_credential"], private, sealed["seal"]["info"] + "x")

        second = await client.post(collect, headers=lane["pat"])
        assert (second.status_code, second.json()["code"]) == (409, "sso_operation_request_already_collected")

        operation = await lane["pool"].fetchrow("SELECT * FROM sso_identity_operations")
        assert operation["operation_id"] == uuid.UUID(request["request"]["grant"]["operation_id"])
        assert operation["credential_hash"] == hashlib.sha256(credential.encode()).hexdigest()
        assert (operation["granted_by"], operation["actor_id"], operation["actor_token_id"]) == (lane["human"], lane["machine"], lane["tid"])
        assert operation["expires_at"] == datetime.fromisoformat(decided["operation_expires_at"])
        assert json.loads(operation["provider_configure_summary"]) == SUMMARY

        path = f"/api/v1/admin/sso/identity-operations/{operation['operation_id']}/execute"
        carrier = {"Authorization": "AKB-Operation " + credential}
        configured = await client.post(path, headers=carrier, json=execute_body("configure", PAYLOAD))
        assert configured.status_code == 200, configured.text
        assert configured.json()["provider"]["client_id"] == "akb-broker-next"
        enabled = await client.post(path, headers=carrier, json=execute_body("enable", {}))
        assert enabled.status_code == 200 and enabled.json()["provider"]["enabled"]
        assert lane["control"].configured_secret == SECRET

        after = await client.get("/api/v1/admin/sso/providers/workforce/ownership", headers=lane["pat"])
        assert after.json()["provider_secret_revision"] == "secret-v2"  # pragma: allowlist secret
        human_texts.append((await client.get(BASE)).text)
        human_texts.append((await client.get(f"{BASE}/{request['request_id']}")).text)
        human_texts.append((await client.get(f"/api/v1/admin/sso/identity-operations/{operation['operation_id']}")).text)
        requester_view = (await client.get(f"{BASE}/{request['request_id']}", headers=lane["pat"])).json()
        assert requester_view["status"] == "collected"

    assert [kind for kind, _ in await events(lane)] == [
        "auth.sso_operation_request_proposed", "auth.sso_operation_request_approved",
        "auth.sso_operation_request_credential_issued"]
    scanned = [*human_texts, repr(requester_view), caplog.text]
    for table in ("events", "sso_operation_requests", "sso_identity_operations", "sso_identity_operation_requests",
                  "sso_provider_ownership"):
        scanned.append(repr(await lane["pool"].fetch(f"SELECT * FROM {table}")))
    for text in scanned:
        assert credential not in text and sealed["sealed_credential"] not in text and SECRET not in text


async def test_execution_refuses_a_payload_that_differs_from_the_approved_summary(lane):
    owned = await own(lane)
    private, public = keypair()
    # The requester commits to one payload and shows the human another summary.
    hidden = {**PAYLOAD, "issuer": "https://attacker.example/realms/x",
              "discovery_url": "https://attacker.example/realms/x/.well-known/openid-configuration"}
    body = grant_body(public, owned["revision"], verbs=["configure"],
                      provider_payload_sha256={"configure": ops._digest(hidden)})
    async with review_http_client() as client:
        csrf = await review_human_cookies(client)
        request = await propose_and_approve(client, lane, csrf, body)
        sealed = (await client.post(f"{BASE}/{request['request_id']}/credential", headers=lane["pat"])).json()
        credential = unseal(sealed["sealed_credential"], private, sealed["seal"]["info"])
        path = f"/api/v1/admin/sso/identity-operations/{sealed['operation_id']}/execute"
        refused = await client.post(path, headers={"Authorization": "AKB-Operation " + credential},
                                    json=execute_body("configure", hidden))
        assert (refused.status_code, refused.json()["code"]) == (403, "sso_operation_summary_conflict")
        # The approved summary itself does not match the approved digest either.
        mismatch = await client.post(path, headers={"Authorization": "AKB-Operation " + credential},
                                     json=execute_body("configure", PAYLOAD))
        assert (mismatch.status_code, mismatch.json()["code"]) == (403, "sso_operation_payload_conflict")
    assert lane["control"].providers[0].issuer == OLD and lane["control"].configured_secret is None


async def test_collection_and_execution_end_at_approval_time_plus_ttl(lane):
    owned = await own(lane)
    pool = lane["pool"]
    private, public = keypair()
    async with review_http_client() as client:
        csrf = await review_human_cookies(client)
        late = await propose_and_approve(client, lane, csrf, grant_body(public, owned["revision"], ttl_seconds=60))
        await pool.execute("""UPDATE sso_operation_requests SET decided_at=NOW()-INTERVAL '61 seconds',
            operation_expires_at=NOW()-INTERVAL '1 second' WHERE request_id=$1""", uuid.UUID(late["request_id"]))
        expired = await client.post(f"{BASE}/{late['request_id']}/credential", headers=lane["pat"])
        assert (expired.status_code, expired.json()["code"]) == (403, "sso_operation_request_expired")
        assert (await client.get(f"{BASE}/{late['request_id']}")).json()["status"] == "expired"

        # The window counts from approval, not from collection.
        aged = await propose_and_approve(client, lane, csrf, grant_body(public, owned["revision"]))
        await pool.execute("""UPDATE sso_operation_requests SET decided_at=NOW()-INTERVAL '590 seconds',
            operation_expires_at=NOW()+INTERVAL '10 seconds' WHERE request_id=$1""", uuid.UUID(aged["request_id"]))
        sealed = (await client.post(f"{BASE}/{aged['request_id']}/credential", headers=lane["pat"])).json()
        expires = await pool.fetchval("SELECT expires_at FROM sso_identity_operations WHERE operation_id=$1",
                                      uuid.UUID(sealed["operation_id"]))
        assert expires == await pool.fetchval("SELECT operation_expires_at FROM sso_operation_requests WHERE request_id=$1",
                                              uuid.UUID(aged["request_id"]))
        assert expires - datetime.now(timezone.utc) < timedelta(seconds=11)
        credential = unseal(sealed["sealed_credential"], private, sealed["seal"]["info"])
        await pool.execute("""UPDATE sso_identity_operations SET created_at=clock_timestamp()-INTERVAL '600 seconds',
            expires_at=clock_timestamp()-INTERVAL '1 second' WHERE operation_id=$1""", uuid.UUID(sealed["operation_id"]))
        after = await client.post(f"/api/v1/admin/sso/identity-operations/{sealed['operation_id']}/execute",
                                  headers={"Authorization": "AKB-Operation " + credential},
                                  json=execute_body("configure", PAYLOAD))
        assert (after.status_code, after.json()["code"]) == (403, "sso_operation_retired")

        # A request nobody decided expires on its own, once.
        pending = (await client.post(BASE, headers=lane["pat"], json=grant_body(public, owned["revision"]))).json()
        await pool.execute("""UPDATE sso_operation_requests SET created_at=clock_timestamp()-INTERVAL '3601 seconds',
            expires_at=clock_timestamp()-INTERVAL '1 second' WHERE request_id=$1""", uuid.UUID(pending["request_id"]))
        statuses = {item["request_id"]: item["status"] for item in (await client.get(BASE)).json()["requests"]}
        assert statuses[pending["request_id"]] == "expired"
        stale = await client.post(f"{BASE}/{pending['request_id']}/approve", headers=csrf,
                                  json={"expected_request_sha256": pending["request_sha256"]})
        assert (stale.status_code, stale.json()["code"]) == (409, "sso_operation_request_not_pending")
        assert (await client.get(f"{BASE}/{pending['request_id']}")).json()["status"] == "expired"
    expired_events = [payload for kind, payload in await events(lane) if kind == "auth.sso_operation_request_expired"]
    assert sorted((item["request_id"], item["approved"]) for item in expired_events) == sorted(
        [(late["request_id"], True), (pending["request_id"], False)])
    assert not await lane["pool"].fetchval("SELECT EXISTS(SELECT 1 FROM sso_identity_operations WHERE operation_id=$1)",
                                           uuid.UUID(late["request"]["grant"]["operation_id"]))


async def test_collection_after_the_provider_revision_changed_is_refused(lane):
    owned = await own(lane)
    _, public = keypair()
    async with review_http_client() as client:
        csrf = await review_human_cookies(client)
        request = await propose_and_approve(client, lane, csrf, grant_body(public, owned["revision"]))
        # A human emergency disable advances the revision after the approval.
        disabled = await client.post("/api/v1/admin/sso/providers/workforce/disable", headers=csrf)
        assert disabled.status_code == 200, disabled.text
        refused = await client.post(f"{BASE}/{request['request_id']}/credential", headers=lane["pat"])
        assert (refused.status_code, refused.json()["code"]) == (409, "sso_provider_revision_conflict")
        assert (await client.get(f"{BASE}/{request['request_id']}")).json()["status"] == "approved"
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_identity_operations") == 0


async def test_demoting_the_approver_or_revoking_the_token_blocks_collection_and_execution(lane):
    owned = await own(lane)
    pool = lane["pool"]
    private, public = keypair()
    async with review_http_client() as client:
        csrf = await review_human_cookies(client)
        held = await propose_and_approve(client, lane, csrf, grant_body(public, owned["revision"]))
        used = await propose_and_approve(client, lane, csrf, grant_body(public, owned["revision"]))
        sealed = (await client.post(f"{BASE}/{used['request_id']}/credential", headers=lane["pat"])).json()
        credential = unseal(sealed["sealed_credential"], private, sealed["seal"]["info"])
        path = f"/api/v1/admin/sso/identity-operations/{sealed['operation_id']}/execute"
        carrier = {"Authorization": "AKB-Operation " + credential}

        await pool.execute("UPDATE users SET is_admin=FALSE WHERE id=$1", lane["human"])
        demoted = await client.post(f"{BASE}/{held['request_id']}/credential", headers=lane["pat"])
        assert (demoted.status_code, demoted.json()["code"]) == (403, "sso_operation_actor_retired")
        blocked = await client.post(path, headers=carrier, json=execute_body("configure", PAYLOAD))
        assert (blocked.status_code, blocked.json()["code"]) == (403, "sso_operation_actor_retired")
        await pool.execute("UPDATE users SET is_admin=TRUE WHERE id=$1", lane["human"])

        await pool.execute("DELETE FROM tokens WHERE id=$1", lane["tid"])
        revoked = await client.post(f"{BASE}/{held['request_id']}/credential", headers=lane["pat"])
        assert revoked.status_code == 401
        blocked = await client.post(path, headers=carrier, json=execute_body("configure", PAYLOAD))
        assert (blocked.status_code, blocked.json()["code"]) == (403, "sso_operation_actor_retired")
    assert lane["control"].configured_secret is None
    assert await pool.fetchval("SELECT COUNT(*) FROM sso_identity_operations") == 1


async def test_a_revoked_requester_cannot_have_its_proposal_approved(lane):
    before = await ops.ownership("workforce")
    async with review_http_client() as client:
        csrf = await review_human_cookies(client)
        proposed = (await client.post(BASE, headers=lane["pat"], json={
            "kind": "ownership_claim", "provider_alias": "workforce", "owner_id": OWNER,
            "expected_revision": before["revision"], "expected_fingerprint": before["fingerprint"]})).json()
        await lane["pool"].execute("DELETE FROM tokens WHERE id=$1", lane["tid"])
        refused = await client.post(f"{BASE}/{proposed['request_id']}/approve", headers=csrf,
                                    json={"expected_request_sha256": proposed["request_sha256"]})
        assert (refused.status_code, refused.json()["code"]) == (403, "sso_operation_actor_retired")
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_provider_ownership") == 0


async def test_deny_and_cancel_close_a_request_for_good(lane):
    owned = await own(lane)
    _, public = keypair()
    async with review_http_client() as client:
        csrf = await review_human_cookies(client)
        first = (await client.post(BASE, headers=lane["pat"], json=grant_body(public, owned["revision"]))).json()
        deny = f"{BASE}/{first['request_id']}/deny"
        assert (await client.post(deny, json={"expected_request_sha256": first["request_sha256"]})).status_code == 401
        wrong = await client.post(deny, headers=csrf, json={"expected_request_sha256": "f" * 64})
        assert (wrong.status_code, wrong.json()["code"]) == (409, "sso_operation_request_digest_conflict")
        denied = await client.post(deny, headers=csrf, json={"expected_request_sha256": first["request_sha256"]})
        assert denied.status_code == 200 and denied.json()["status"] == "denied"
        late = await client.post(f"{BASE}/{first['request_id']}/credential", headers=lane["pat"])
        assert (late.status_code, late.json()["code"]) == (409, "sso_operation_request_not_approved")

        second = (await client.post(BASE, headers=lane["pat"], json=grant_body(public, owned["revision"]))).json()
        cancel = f"{BASE}/{second['request_id']}/cancel"
        foreign = await client.post(cancel, headers=lane["other_pat"])
        assert (foreign.status_code, foreign.json()["code"]) == (403, "sso_operation_request_scope_forbidden")
        assert (await client.post(cancel, headers=csrf)).status_code == 403
        cancelled = await client.post(cancel, headers=lane["pat"])
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        refused = await client.post(f"{BASE}/{second['request_id']}/approve", headers=csrf,
                                    json={"expected_request_sha256": second["request_sha256"]})
        assert (refused.status_code, refused.json()["code"]) == (409, "sso_operation_request_not_pending")

        # An approved grant can still be withdrawn by its requester before collection.
        third = await propose_and_approve(client, lane, csrf, grant_body(public, owned["revision"]))
        assert (await client.post(f"{BASE}/{third['request_id']}/cancel", headers=lane["pat"])).json()["status"] == "cancelled"
        gone = await client.post(f"{BASE}/{third['request_id']}/credential", headers=lane["pat"])
        assert (gone.status_code, gone.json()["code"]) == (409, "sso_operation_request_not_approved")
    kinds = [kind for kind, _ in await events(lane)]
    assert kinds.count("auth.sso_operation_request_denied") == 1
    assert kinds.count("auth.sso_operation_request_cancelled") == 2
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_identity_operations") == 0


@pytest.mark.parametrize(("changes", "status", "code"), [
    ({"verbs": ["configure", "apply"]}, 422, "sso_operation_request_invalid"),
    ({"cohort": []}, 422, "sso_operation_request_invalid"),
    ({"actor_token_id": str(uuid.uuid4())}, 422, "sso_operation_request_invalid"),
    ({"ttl_seconds": 601}, 422, "sso_operation_request_invalid"),
    ({"configure_summary": None}, 422, "sso_operation_scope_invalid"),
    ({"verbs": ["enable"], "provider_payload_sha256": {"enable": ops._digest({})}}, 422, "sso_operation_scope_invalid"),
    ({"provider_payload_sha256": {"configure": ops._digest(PAYLOAD)}}, 422, "sso_operation_scope_invalid"),
    ({"recipient_public_key": base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode()}, 422, "sso_operation_recipient_key_invalid"),
    ({"recipient_public_key": "A" * 42 + "B"}, 422, "sso_operation_recipient_key_invalid"),
    ({"configure_summary": {**SUMMARY, "display_name": "Company\nSSO"}}, 422, "sso_operation_request_invalid"),
    ({"provider_alias": " workforce"}, 422, "sso_operation_request_invalid"),
])
async def test_proposals_outside_the_contract_are_refused_without_a_row(lane, changes, status, code):
    _, public = keypair()
    async with review_http_client() as client:
        refused = await client.post(BASE, headers=lane["pat"], json=grant_body(public, 1, **changes))
        body = refused.json()
        assert (refused.status_code, body.get("code") or body["detail"]["code"]) == (status, code)
    assert await lane["pool"].fetchval("SELECT COUNT(*) FROM sso_operation_requests") == 0


async def test_an_operation_id_can_be_requested_once(lane):
    _, public = keypair()
    body = grant_body(public, 1)
    async with review_http_client() as client:
        assert (await client.post(BASE, headers=lane["pat"], json=body)).status_code == 201
        again = await client.post(BASE, headers=lane["pat"], json=body)
        assert (again.status_code, again.json()["code"]) == (409, "sso_operation_id_conflict")


async def test_an_installation_with_no_requests_lists_none(lane):
    async with review_http_client() as client:
        assert (await client.get(BASE)).status_code == 401
        await review_human_cookies(client)
        listed = await client.get(BASE)
        assert listed.status_code == 200 and listed.json() == {"schema_version": 1, "requests": []}
        missing = await client.get(f"{BASE}/{uuid.uuid4()}")
        assert (missing.status_code, missing.json()["code"]) == (404, "sso_operation_request_not_found")


async def test_a_human_reauthorization_keeps_the_approved_summary(lane):
    from app.sso import operation_attempts

    owned = await own(lane)
    _, public = keypair()
    async with review_http_client() as client:
        csrf = await review_human_cookies(client)
        request = await propose_and_approve(client, lane, csrf, grant_body(public, owned["revision"]))
        sealed = (await client.post(f"{BASE}/{request['request_id']}/credential", headers=lane["pat"])).json()
    parent = await lane["pool"].fetchrow("SELECT * FROM sso_identity_operations WHERE operation_id=$1",
                                         uuid.UUID(sealed["operation_id"]))
    child_id = uuid.uuid4()
    await operation_attempts.reauthorize(parent["operation_id"], operation_id=child_id,
                                         expected_intent_sha256=operation_attempts.intent_hash(parent),
                                         ttl_seconds=600, granted_by=lane["human"])
    child = await lane["pool"].fetchrow("SELECT * FROM sso_identity_operations WHERE operation_id=$1", child_id)
    assert json.loads(child["provider_configure_summary"]) == SUMMARY
