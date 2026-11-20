"""Nonce replay and requester history against private PostgreSQL and HTTP."""

import asyncio
import uuid

import pytest

from app.config import settings
from app.sso import identity_operations as ops
from tests.test_sso_identity_operations_postgres import review_http_client, review_human_cookies
from tests.test_sso_operation_requests_postgres import BASE, OWNER, digest, events, grant_body, keypair, own
from tests.test_sso_operation_requests_postgres import lane as request_lane_fixture


lane = request_lane_fixture
pytestmark = pytest.mark.asyncio


async def claim_body():
    current = await ops.ownership("workforce")
    return {"kind": "ownership_claim", "provider_alias": "workforce", "owner_id": OWNER,
            "expected_revision": current["revision"], "expected_fingerprint": current["fingerprint"]}


async def test_nonce_replay_is_exact_concurrent_global_and_retained_after_cancel(lane, monkeypatch):
    body = {**await claim_body(), "request_nonce": str(uuid.uuid4())}
    body["expected_target_id"] = (await ops.ownership("workforce"))["target_id"]
    async with review_http_client() as client:
        first, second = await asyncio.gather(*[client.post(BASE, headers=lane["pat"], json=body) for _ in range(2)])
        assert first.status_code == second.status_code == 201
        created, replay = first.json(), second.json()
        assert created == replay and created["request"]["request_nonce"] == body["request_nonce"]
        assert created["request_sha256"] == digest(created["request"])
        assert await lane["pool"].fetchval("SELECT count(*) FROM sso_operation_requests") == 1
        assert len(await events(lane)) == 1
        noncanonical = await client.post(BASE, headers=lane["pat"], json={**body, "request_nonce": body["request_nonce"].replace("-", "")})
        assert noncanonical.status_code == 422
        changed = await client.post(BASE, headers=lane["pat"], json={**body, "owner_id": "other-owner"})
        assert (changed.status_code, changed.json()["code"]) == (409, "sso_operation_request_nonce_conflict")
        foreign = await client.post(BASE, headers=lane["other_pat"], json=body)
        assert (foreign.status_code, foreign.json()["code"]) == (409, "sso_operation_request_nonce_conflict")
        wrong_target = await client.post(BASE, headers=lane["pat"], json={**body, "expected_target_id": str(uuid.uuid4())})
        assert (wrong_target.status_code, wrong_target.json()["code"]) == (409, "sso_management_target_conflict")
        monkeypatch.setattr(settings, "auth_runtime_generation", 2)
        changed_source = await client.post(BASE, headers=lane["pat"], json=body)
        assert (changed_source.status_code, changed_source.json()["code"]) == (409, "sso_operation_request_nonce_conflict")
        monkeypatch.setattr(settings, "auth_runtime_generation", 1)
        cancelled = await client.post(f"{BASE}/{created['request_id']}/cancel", headers=lane["pat"])
        assert cancelled.status_code == 200
        replay = await client.post(BASE, headers=lane["pat"], json=body)
        assert replay.status_code == 201 and replay.json() == cancelled.json()
        assert replay.json()["created_at"] == created["created_at"] and replay.json()["expires_at"] == created["expires_at"]
        assert len(await events(lane)) == 2


async def test_legacy_document_digest_is_unchanged_by_optional_nonce(lane):
    async with review_http_client() as client:
        created = await client.post(BASE, headers=lane["pat"], json=await claim_body())
        assert created.status_code == 201
        obj = created.json()
        assert "request_nonce" not in obj["request"] and obj["request_sha256"] == digest(obj["request"])
        assert (await client.get(f"{BASE}/{obj['request_id']}", headers=lane["pat"])).json() == obj
        listing = (await client.get(BASE, headers=lane["pat"])).json()
        assert "next_cursor" not in listing and listing["requests"][0]["request"] == obj["request"]
        csrf = await review_human_cookies(client)
        approved = await client.post(f"{BASE}/{obj['request_id']}/approve", headers=csrf,
                                     json={"expected_request_sha256": obj["request_sha256"]})
        assert approved.status_code == 200 and "request_nonce" not in approved.json()["request"]
        assert approved.json()["request_sha256"] == digest(approved.json()["request"])


async def test_requester_keyset_pages_keep_all_history_despite_status_changes(lane):
    body = await claim_body()
    async with review_http_client() as client:
        ids = []
        for _ in range(105):
            response = await client.post(BASE, headers=lane["pat"], json=body)
            assert response.status_code == 201
            ids.append(response.json()["request_id"])
        first = (await client.get(BASE, params={"page_size": 17}, headers=lane["pat"])).json()
        assert len(first["requests"]) == 17 and first["next_cursor"] == first["requests"][-1]["request_id"]
        # A status change moves an item in legacy rank order but not the
        # immutable (created_at, request_id) cursor order.
        assert (await client.post(f"{BASE}/{ids[0]}/cancel", headers=lane["pat"])).status_code == 200
        seen = [item["request_id"] for item in first["requests"]]
        cursor = first["next_cursor"]
        while cursor is not None:
            page = (await client.get(BASE, params={"page_size": 17, "cursor": cursor}, headers=lane["pat"])).json()
            seen.extend(item["request_id"] for item in page["requests"])
            if page["next_cursor"] is not None:
                assert page["next_cursor"] == page["requests"][-1]["request_id"] != cursor
            cursor = page["next_cursor"]
        assert len(seen) == len(set(seen)) == len(ids) and set(seen) == set(ids)
        foreign = await client.get(BASE, params={"page_size": 17, "cursor": ids[0]}, headers=lane["other_pat"])
        assert (foreign.status_code, foreign.json()["code"]) == (422, "sso_operation_request_cursor_invalid")
        legacy = (await client.get(BASE, headers=lane["pat"])).json()
        assert len(legacy["requests"]) == 100 and "next_cursor" not in legacy
        assert all(item["status"] == "pending" for item in legacy["requests"])


async def test_nonce_grant_replays_terminal_state_without_second_credential(lane):
    current = await own(lane)
    _, public_key = keypair()
    body = grant_body(public_key, current["revision"], request_nonce=str(uuid.uuid4()),
                      expected_target_id=current["target_id"])
    async with review_http_client() as client:
        created = await client.post(BASE, headers=lane["pat"], json=body)
        assert created.status_code == 201
        request = created.json()
        assert request["request_sha256"] == digest(request["request"])
        csrf = await review_human_cookies(client)
        approved = await client.post(f"{BASE}/{request['request_id']}/approve", headers=csrf,
                                     json={"expected_request_sha256": request["request_sha256"]})
        assert approved.status_code == 200
        assert (await client.post(BASE, headers=lane["pat"], json=body)).json() == approved.json()
        issued = await client.post(f"{BASE}/{request['request_id']}/credential", headers=lane["pat"])
        assert issued.status_code == 200
        replay = await client.post(BASE, headers=lane["pat"], json=body)
        assert replay.status_code == 201 and replay.json()["status"] == "collected"
        assert "sealed_credential" not in replay.text and "credential" not in replay.json()
        again = await client.post(f"{BASE}/{request['request_id']}/credential", headers=lane["pat"])
        assert (again.status_code, again.json()["code"]) == (409, "sso_operation_request_already_collected")
        assert await lane["pool"].fetchval("SELECT count(*) FROM sso_operation_requests") == 1
        assert [kind for kind, _ in await events(lane)] == ["auth.sso_operation_request_proposed",
            "auth.sso_operation_request_approved", "auth.sso_operation_request_credential_issued"]


async def test_realm_bound_claim_refuses_replacement_before_ownership_cas(lane, monkeypatch):
    realm = {"id": str(uuid.uuid4())}

    async def read_realm():
        return {"realm_id": realm["id"], "realm_name": "akb", "issuer": "https://auth.akb.example.com/realms/akb"}

    monkeypatch.setattr(lane["control"], "read_realm_identity", read_realm)
    before = await ops.ownership("workforce")
    original = realm["id"]
    body = {**await claim_body(), "request_nonce": str(uuid.uuid4()),
            "expected_target_id": before["target_id"], "expected_realm_id": original}
    async with review_http_client() as client:
        bad = await client.post(BASE, headers=lane["pat"], json={**body, "expected_realm_id": str(uuid.uuid4())})
        assert (bad.status_code, bad.json()["code"]) == (409, "sso_management_realm_conflict")
        proposed = await client.post(BASE, headers=lane["pat"], json=body)
        assert proposed.status_code == 201
        request = proposed.json()
        assert request["request"]["expected_realm_id"] == original
        assert request["request_sha256"] == digest(request["request"])
        csrf = await review_human_cookies(client)
        realm["id"] = str(uuid.uuid4())
        refused = await client.post(f"{BASE}/{request['request_id']}/approve", headers=csrf,
                                    json={"expected_request_sha256": request["request_sha256"]})
        assert (refused.status_code, refused.json()["code"]) == (409, "sso_management_realm_conflict")
        assert await lane["pool"].fetchval("SELECT count(*) FROM sso_provider_ownership") == 0
        assert (await client.get(f"{BASE}/{request['request_id']}", headers=lane["pat"])).json()["status"] == "pending"
        realm["id"] = original

        # Replacement can also happen after the initial approval read but
        # before the ownership row is written.
        original_provider = ops._provider

        async def replace_after_provider(*args, **kwargs):
            result = await original_provider(*args, **kwargs)
            realm["id"] = str(uuid.uuid4())
            return result

        monkeypatch.setattr(ops, "_provider", replace_after_provider)
        raced = await client.post(f"{BASE}/{request['request_id']}/approve", headers=csrf,
                                  json={"expected_request_sha256": request["request_sha256"]})
        assert (raced.status_code, raced.json()["code"]) == (409, "sso_management_realm_conflict")
        assert await lane["pool"].fetchval("SELECT count(*) FROM sso_provider_ownership") == 0
        monkeypatch.setattr(ops, "_provider", original_provider)
        realm["id"] = original
        approved = await client.post(f"{BASE}/{request['request_id']}/approve", headers=csrf,
                                     json={"expected_request_sha256": request["request_sha256"]})
        assert approved.status_code == 200 and approved.json()["status"] == "approved"


async def test_realm_bound_grant_checks_collection_and_immediate_provider_write(lane, monkeypatch):
    from tests.test_sso_operation_requests_postgres import PAYLOAD, SECRET, execute_body, unseal

    realm = {"id": str(uuid.uuid4()), "reads": 0, "replace_on": 0}

    async def read_realm():
        realm["reads"] += 1
        current = str(uuid.uuid4()) if realm["replace_on"] and realm["reads"] >= realm["replace_on"] else realm["id"]
        return {"realm_id": current, "realm_name": "akb", "issuer": "https://auth.akb.example.com/realms/akb"}

    monkeypatch.setattr(lane["control"], "read_realm_identity", read_realm)
    owned = await own(lane)
    private, public = keypair()
    original = realm["id"]
    body = grant_body(public, owned["revision"], request_nonce=str(uuid.uuid4()),
                      expected_target_id=owned["target_id"], expected_realm_id=original)
    async with review_http_client() as client:
        proposed = await client.post(BASE, headers=lane["pat"], json=body)
        assert proposed.status_code == 201
        request = proposed.json()
        assert request["request"]["expected_realm_id"] == original
        csrf = await review_human_cookies(client)
        approved = await client.post(f"{BASE}/{request['request_id']}/approve", headers=csrf,
                                     json={"expected_request_sha256": request["request_sha256"]})
        assert approved.status_code == 200
        realm["id"] = str(uuid.uuid4())
        path = f"{BASE}/{request['request_id']}/credential"
        refused = await client.post(path, headers=lane["pat"])
        assert (refused.status_code, refused.json()["code"]) == (409, "sso_management_realm_conflict")
        assert await lane["pool"].fetchval("SELECT count(*) FROM sso_identity_operations") == 0
        realm["id"] = original
        original_provider = ops._provider

        async def replace_during_collection(*args, **kwargs):
            result = await original_provider(*args, **kwargs)
            realm["id"] = str(uuid.uuid4())
            return result

        monkeypatch.setattr(ops, "_provider", replace_during_collection)
        raced = await client.post(path, headers=lane["pat"])
        assert (raced.status_code, raced.json()["code"]) == (409, "sso_management_realm_conflict")
        assert await lane["pool"].fetchval("SELECT count(*) FROM sso_identity_operations") == 0
        monkeypatch.setattr(ops, "_provider", original_provider)
        realm["id"] = original
        issued = await client.post(path, headers=lane["pat"])
        assert issued.status_code == 200
        sealed = issued.json()
        credential = unseal(sealed["sealed_credential"], private, sealed["seal"]["info"])
        operation = await lane["pool"].fetchrow("SELECT * FROM sso_identity_operations")
        assert operation["target_realm_id"] == original

        # The first execute boundary sees the original realm; replacement
        # after that read must still fail immediately before the REST write.
        realm["reads"], realm["replace_on"] = 0, 2
        effect = await client.post(
            f"/api/v1/admin/sso/identity-operations/{operation['operation_id']}/execute",
            headers={"Authorization": "AKB-Operation " + credential},
            json=execute_body("configure", PAYLOAD),
        )
        assert (effect.status_code, effect.json()["code"]) == (409, "sso_management_realm_conflict")
        assert lane["control"].configured_secret is None
        assert await lane["pool"].fetchval("SELECT count(*) FROM sso_identity_operation_requests") == 0
        assert SECRET not in effect.text and credential not in effect.text
