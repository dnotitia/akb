"""Required HTTP proof of deletion, retained history and real actor boundaries.

The repository runtime owns reset, credentials and disposable audit history.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import httpx
import pytest

from .conftest import _prepare_secondary_user
from .runtime import RuntimeContext


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
def adoption_clients(runtime_session: RuntimeContext):
    origin = runtime_session.descriptor.app_origin
    _name, owner, _secrets = _prepare_secondary_user(runtime_session)
    _name, reader, _secrets = _prepare_secondary_user(runtime_session)
    admin = runtime_session.pat
    with (
        httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + owner},
                     trust_env=False, timeout=30) as member,
        httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + admin},
                     trust_env=False, timeout=30) as operator,
        httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + reader},
                     trust_env=False, timeout=30) as viewer,
    ):
        assert _ok(member.get("/api/v1/auth/me"))["is_admin"] is False
        assert _ok(operator.get("/api/v1/auth/me"))["is_admin"] is True
        yield member, operator, viewer


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.mark.parametrize("state", ["planned", "applied", "blocked"])
def test_delete_retains_evidence_and_never_resurrects_vault(adoption_clients, state):
    member, operator, viewer = adoption_clients
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
        assert target["vault_deleted"] is False
        reader_id = _ok(viewer.get("/api/v1/auth/me"))["user_id"]
        _ok(member.post(f"/api/v1/vaults/{vault}/grant",
                        json={"user_id": reader_id, "role": "reader"}))
        assert viewer.delete("/api/v1/vaults/" + vault).status_code == 403
        assert viewer.get(item).status_code == 404
        assert viewer.post(item + "/apply").status_code == 404
        assert viewer.post(path, json=body, headers=headers).status_code == 403
        # A denied delete leaves the plan, schema and owner's authority intact.
        assert _ok(member.get(item))["targets"] == plan["targets"]
        _ok(member.get(f"/api/v1/tables/{vault}/orders/schema"))
        deleted = member.delete("/api/v1/vaults/" + vault)
        assert deleted.status_code == 200, f"{vault}: {deleted.text}"
        assert member.get(f"/api/v1/tables/{vault}/orders/schema").status_code in (403, 404)
        retained = _ok(operator.get(item))
        assert retained["targets"][0] == {**target, "vault_deleted": True}
        assert retained["input_digest"] == plan["input_digest"]
        assert member.get(item).status_code == 404
        assert member.post(item + "/apply").status_code == 404
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
        # Recreating a name must not confer access to the old Vault's ledger.
        assert member.get(item).status_code == 404
        assert member.post(item + "/apply").status_code == 404
        _ok(member.delete("/api/v1/vaults/" + vault))
    finally:
        # The runtime reset also cleans fixtures after a failed assertion.
        cleanup = member.delete("/api/v1/vaults/" + vault)
        assert cleanup.status_code in (200, 404), f"test cleanup retained {vault}: {cleanup.text}"
