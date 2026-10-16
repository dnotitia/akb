"""Opt-in real API proof for Vault deletion and immutable adoption history.

Set AKB_ADOPTION_TEST_URL, AKB_E2E_PAT (Vault owner), and AKB_E2E_ADMIN_PAT
(registry/history operator). Only randomly named test Vaults are deleted.
App registry and adoption history are intentionally retained, as in production.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import uuid

import httpx
import pytest


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def _release(app_key):
    table = {"name": "orders", "columns": [{"name": "amount", "type": "numeric"}],
             "unique_keys": [], "indexes": []}
    step = {"id": "create_orders", "phase": "expand", "operation": "create_table",
            "payload": {"table": "orders", **{k: v for k, v in table.items() if k != "name"}}}
    manifest = {
        "manifest_version": 2, "app_key": app_key, "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64, "schema_version": 1,
        "schema": {"tables": [table], "fingerprint": _digest([table])},
        "transition_plans": [{"source": "fresh", "steps": [step]}],
    }
    checksum = _digest({**manifest, "product_version": "1.0.0"})
    manifest["transition_plans"][0]["steps"] = [{**step, "checksum": _digest(step)}]
    return {"version": "1.0.0", "manifest": manifest, "manifest_checksum": checksum}


@pytest.fixture
def adoption_clients():
    origin = os.getenv("AKB_ADOPTION_TEST_URL")
    if not origin:
        pytest.skip("set AKB_ADOPTION_TEST_URL for actual adoption/delete checks")
    owner, admin = os.getenv("AKB_E2E_PAT"), os.getenv("AKB_E2E_ADMIN_PAT")
    if not owner or not admin:
        pytest.fail("supply owner and system-admin fixture PATs")
    ca_file = os.getenv("AKB_E2E_CA_FILE")
    verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
    with (
        httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + owner},
                     verify=verify, timeout=30) as member,
        httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + admin},
                     verify=verify, timeout=30) as operator,
    ):
        yield member, operator


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.mark.parametrize("state", ["planned", "applied", "blocked"])
def test_delete_retains_evidence_and_never_resurrects_vault(adoption_clients, state):
    member, operator = adoption_clients
    vault = "adoption-delete-test-" + uuid.uuid4().hex[:12]
    vault_id = _ok(member.post("/api/v1/vaults", params={"name": vault}))["vault_id"]
    try:
        columns = [{"name": "amount", "type": "numeric"}]
        if state == "blocked":
            columns.append({"name": "unexpected", "type": "text"})
        _ok(member.post(f"/api/v1/tables/{vault}", json={"name": "orders", "columns": columns}))
        app_key = "adoption-delete-" + uuid.uuid4().hex
        app = _ok(operator.post("/api/v1/apps", json={"app_key": app_key}))
        app_id = app["id"]
        release = _ok(operator.post(f"/api/v1/apps/{app_id}/releases", json=_release(app_key)))
        path = f"/api/v1/apps/{app_id}/legacy-adoptions"
        body = {"baseline_release_id": release["id"],
                "targets": [{"vault_id": vault_id, "table_allowlist": ["orders"]}]}
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        plan = _ok(member.post(path, json=body, headers=headers), 201)
        item = path + "/" + plan["adoption_id"]
        if state == "applied":
            plan = _ok(member.post(item + "/apply"))
        assert plan["targets"][0]["state"] == state
        target = plan["targets"][0]
        deleted = member.delete("/api/v1/vaults/" + vault)
        assert deleted.status_code == 200, f"{vault}: {deleted.text}"
        assert member.get(f"/api/v1/tables/{vault}/orders/schema").status_code in (403, 404)
        retained = _ok(operator.get(item))
        assert retained["targets"][0] == {**target, "vault_deleted": True}
        assert retained["input_digest"] == plan["input_digest"]
        for _ in range(2):
            for response in (
                operator.post(item + "/apply"),
                operator.post(path, json=body, headers=headers),
            ):
                assert response.status_code == 409, response.text
                assert "new adoption plan" in response.text
        recreated = _ok(member.post("/api/v1/vaults", params={"name": vault}))
        assert recreated["vault_id"] != vault_id
        assert _ok(operator.get(item))["targets"] == retained["targets"]
        me = _ok(member.get("/api/v1/auth/me"))
        if not me.get("is_admin"):
            # Recreating a name must not confer access to the old Vault's ledger.
            assert member.get(item).status_code == 404
        _ok(member.delete("/api/v1/vaults/" + vault))
    finally:
        # Do not mask a failed request with another request on a connection
        # the server closed. Leave that exact fixture for diagnosis/cleanup.
        if sys.exception() is None:
            cleanup = member.delete("/api/v1/vaults/" + vault)
            assert cleanup.status_code in (200, 404), f"test cleanup retained {vault}: {cleanup.text}"
