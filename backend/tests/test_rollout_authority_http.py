"""Opt-in real-worker rollout lifecycle and recovery authorization proof.

Set AKB_ROLLOUT_TEST_URL, AKB_E2E_PAT (Vault owner) and AKB_E2E_ADMIN_PAT.
Creates only random test Vaults/apps. Registry/rollout audit history is retained.
No SQL, worker controls, extra authority anchors, or timeout changes are used.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import ssl
import time
import uuid

import httpx
import pytest

_CAPABILITIES = ["installation:read", "inventory:read", "rollout:read", "rollout:request"]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _release(app_key, *, required, source=None):
    columns = [{"name": "marker", "type": "text", "required": True}, {"name": "value", "type": "text"}]
    if required:
        columns[1]["required"] = True
    table = {"name": "canary", "columns": columns, "unique_keys": [], "indexes": []}
    create = {"id": "create_canary", "phase": "expand", "operation": "create_table",
              "payload": {"table": "canary", "columns": columns, "unique_keys": [], "indexes": []}}
    create["checksum"] = _digest(create)
    plans = [{"source": "fresh", "steps": [create]}]
    if source:
        enforce = {"id": "enforce_value", "phase": "enforce", "operation": "set_not_null",
                   "payload": {"table": "canary", "column": "value"}}
        enforce["checksum"] = _digest(enforce)
        plans.append({"source": {"release_version": "1.0.0", "schema_fingerprint": source}, "steps": [enforce]})
    version = "1.1.0" if required else "1.0.0"
    manifest = {"manifest_version": 2, "app_key": app_key, "source_revision": "a" * 40,
                "image_digest": "sha256:" + "b" * 64, "schema_version": 2 if required else 1,
                "schema": {"tables": [table], "fingerprint": _digest([table])}, "transition_plans": plans}
    canonical = {**deepcopy(manifest), "product_version": version}
    for plan in canonical["transition_plans"]:
        for step in plan["steps"]:
            step.pop("checksum")
    return {"version": version, "manifest": manifest, "manifest_checksum": _digest(canonical)}


def _ok(response, *statuses):
    assert response.status_code in (statuses or (200,)), response.text
    return response.json()


def _poll(client, path, field, terminal):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        data = _ok(client.get(path))
        if data[field] in terminal:
            return data
        time.sleep(0.2)
    pytest.fail(f"worker did not reach {terminal} for {field}")


@pytest.fixture
def rollout_clients():
    origin = os.getenv("AKB_ROLLOUT_TEST_URL")
    if not origin:
        pytest.skip("set AKB_ROLLOUT_TEST_URL for real worker/HTTP lifecycle checks")
    owner, admin = os.getenv("AKB_E2E_PAT"), os.getenv("AKB_E2E_ADMIN_PAT")
    if not owner or not admin:
        pytest.fail("supply owner and system-admin fixture PATs")
    ca_file = os.getenv("AKB_E2E_CA_FILE")
    verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
    with (
        httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + owner}, verify=verify, timeout=30) as member,
        httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + admin}, verify=verify, timeout=30) as operator,
        httpx.Client(base_url=origin, verify=verify, timeout=30) as app,
    ):
        yield member, operator, app


@pytest.mark.parametrize("installation_count", [1, 2])
def test_real_blocked_upgrade_and_resume_without_an_active_anchor(rollout_clients, installation_count):
    member, operator, app = rollout_clients
    app_key = "rollout-authority-" + uuid.uuid4().hex
    app_id = _ok(operator.post("/api/v1/apps", json={"app_key": app_key}))["id"]
    credential = _ok(operator.post(f"/api/v1/apps/{app_id}/credentials", json={"deployment": "lifecycle-test"}))
    credential_id = credential["credential_id"]
    token = _ok(app.post("/api/v1/auth/app-token", json={"credential": credential["credential"]}))["access_token"]
    app.headers["Authorization"] = "Bearer " + token
    vaults = []
    try:
        baseline = _release(app_key, required=False)
        old = _ok(operator.post(f"/api/v1/apps/{app_id}/releases", json=baseline))
        grants = {}
        for _ in range(installation_count):
            vault = "rollout-authority-test-" + uuid.uuid4().hex[:12]
            vid = _ok(member.post("/api/v1/vaults", params={"name": vault}))["vault_id"]
            vaults.append((vault, vid))
            path = f"/api/v1/apps/{app_id}/installations/{vid}"
            _ok(member.put(path, json={"release_id": old["id"], "capabilities": _CAPABILITIES}), 200, 202)
            installed = _poll(operator, path, "lifecycle", {"active", "blocked"})
            assert installed["lifecycle"] == "active"
            grants[vid] = installed["active_grant"]
            rows = _ok(member.post(f"/api/v1/tables/{vault}/canary/rows", json={"marker": "repair", "value": None},
                                   headers={"Prefer": "return=representation"}), 201)
            assert len(rows["items"]) == 1 and rows["items"][0]["value"] is None
        desired = _release(app_key, required=True, source=baseline["manifest"]["schema"]["fingerprint"])
        new = _ok(operator.post(f"/api/v1/apps/{app_id}/releases", json=desired))
        body = {"release_id": new["id"], "manifest_checksum": new["manifest_checksum"]}
        job = _ok(app.post("/api/v1/app/rollouts", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}), 202)
        app_path = f"/api/v1/app/rollouts/{job['job_id']}"
        # The worker really executes SET NOT NULL against a NULL row.
        source = _poll(app, app_path, "status", {"blocked", "applied"})
        assert source["status"] == "blocked" and source["blocked_reason"] == "step_failed"
        assert len(source["targets"]) == installation_count
        assert any(step["operation"] == "set_not_null" and step["state"] == "failed"
                   for target in source["targets"] for step in target["steps"])
        assert _ok(operator.get(f"/api/v1/apps/{app_id}/rollouts/{job['job_id']}")) == source
        assert app.post("/api/v1/app/authorize", json={"vault_id": vaults[0][1], "capability": "installation:read"}).status_code == 403
        # Rotation invalidates the old app token even during a blocked job.
        rotated = _ok(operator.post(f"/api/v1/apps/{app_id}/credentials/{credential_id}/rotate", json={}))
        credential_id = rotated["credential_id"]
        assert app.get(app_path).status_code == 401
        app.headers.pop("Authorization")
        fresh = _ok(app.post("/api/v1/auth/app-token", json={"credential": rotated["credential"]}))
        app.headers["Authorization"] = "Bearer " + fresh["access_token"]
        assert _ok(app.get(app_path)) == source
        for vault, _vid in vaults:
            repaired = _ok(member.patch(f"/api/v1/tables/{vault}/canary/rows", params={"marker": "eq.repair"},
                                        json={"value": "repaired"}, headers={"Prefer": "return=representation"}))
            assert len(repaired["items"]) == 1 and repaired["items"][0]["value"] == "repaired"
        key = {"Idempotency-Key": str(uuid.uuid4())}
        resumed = _ok(app.post(app_path + "/resume", json=body, headers=key), 202)
        assert resumed["source_rollout_id"] == job["job_id"] and resumed["job_id"] != job["job_id"]
        applied = _poll(app, f"/api/v1/app/rollouts/{resumed['job_id']}", "status", {"applied", "blocked"})
        assert applied["status"] == "applied"
        assert (applied["release_id"], applied["manifest_checksum"]) == (body["release_id"], body["manifest_checksum"])
        assert _ok(app.get(app_path)) == source
        replay = _ok(app.post(app_path + "/resume", json=body, headers=key))
        assert replay["job_id"] == applied["job_id"] and replay["replayed"] is True
        for vault, vid in vaults:
            installed = _ok(operator.get(f"/api/v1/apps/{app_id}/installations/{vid}"))
            assert installed["lifecycle"] == "active" and installed["current_release"]["id"] == new["id"]
            assert installed["active_grant"] == grants[vid]
            rows = _ok(member.get(f"/api/v1/tables/{vault}/canary/rows", params={"marker": "eq.repair"}))
            assert len(rows["items"]) == 1 and rows["items"][0]["value"] == "repaired"
            _ok(member.delete(f"/api/v1/apps/{app_id}/installations/{vid}"), 200, 202)
        assert app.get(app_path).status_code == 403
        assert app.post(app_path + "/resume", json=body, headers=key).status_code == 403
        _ok(operator.delete(f"/api/v1/apps/{app_id}/credentials/{credential_id}"))
        assert app.get(app_path).status_code == 401
    finally:
        # Registry and immutable jobs remain. Only this test's live fixtures
        # are cleaned through normal authorized APIs, never SQL deletion.
        for vault, vid in vaults:
            _ok(member.delete(f"/api/v1/apps/{app_id}/installations/{vid}"), 200, 202)
            _ok(member.delete("/api/v1/vaults/" + vault))
        _ok(operator.delete(f"/api/v1/apps/{app_id}/credentials/{credential_id}"))
