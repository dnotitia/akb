"""Opt-in real HTTP proof for bounded Table reads and exact count contracts.

Set AKB_READ_CAPACITY_TEST_URL and AKB_E2E_PAT. Reader/outsider checks also
require AKB_E2E_READER_PAT and AKB_E2E_OUTSIDER_PAT. Only a newly created,
randomly named fixture Vault is deleted. No production target is assumed.
"""

import os
import ssl
import uuid

import httpx
import pytest


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture(scope="module")
def table_client():
    origin = os.getenv("AKB_READ_CAPACITY_TEST_URL")
    if not origin:
        pytest.skip("set AKB_READ_CAPACITY_TEST_URL for the real HTTP lane")
    token = os.getenv("AKB_E2E_PAT")
    if not token:
        pytest.fail("supply an owner fixture PAT")
    ca = os.getenv("AKB_E2E_CA_FILE")
    verify = ssl.create_default_context(cafile=ca) if ca else True
    vault = "read-capacity-test-" + uuid.uuid4().hex[:12]
    with httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + token},
                      verify=verify, timeout=15) as client:
        _ok(client.post("/api/v1/vaults", params={"name": vault}))
        try:
            base = f"/api/v1/tables/{vault}"
            _ok(client.post(base, json={"name": "records", "columns": [{"name": "label", "type": "text"}]}))
            assert _ok(client.get(base + "/records/schema"))["drift"]["has_drift"] is False
            inserted = _ok(client.post(base + "/records/rows", json=[{"label": f"Row {i}"} for i in range(6)],
                headers={"Prefer": "return=representation"}), 201)["items"]
            yield client, base, vault, inserted
        finally:
            _ok(client.delete("/api/v1/vaults/" + vault))


@pytest.mark.parametrize("counted", [True, False])
def test_catalog_has_exact_default_and_explicit_unmeasured_counts(table_client, counted):
    client, base, _, _ = table_client
    catalog = _ok(client.get(base, params={} if counted else {"include_row_counts": "false"}))
    assert catalog["total"] == 1
    item = catalog["items"][0]
    assert item["name"] == "records" and item["columns"][0]["name"] == "label"
    if counted:
        assert item["row_count"] == 6
    else:
        assert "row_count" not in item


@pytest.mark.parametrize("exact", [True, False])
def test_page_contract_and_count_only_are_distinct(table_client, exact):
    client, base, _, _ = table_client
    path = base + "/records/rows"
    headers = {"Prefer": "count=exact"} if exact else {}
    query = {"limit": 3, "order": "created_at.desc,id.desc"}
    first = _ok(client.get(path, params=query, headers=headers))
    second = _ok(client.get(path, params={**query, "offset": 3}, headers=headers))
    assert first["total"] == second["total"] == (6 if exact else 3)
    assert len({row["id"] for row in first["items"] + second["items"]}) == 6
    # Sentinel uses the same offset stride as visible rows, not limit+1.
    sentinel = _ok(client.get(path, params={**query, "limit": 4}))
    assert sentinel["items"][:3] == first["items"]
    assert sentinel["items"][3] == second["items"][0]
    counted = _ok(client.get(path, params={"limit": 0}, headers={"Prefer": "count=exact"}))
    assert counted["total"] == 6 and counted["items"] == []
    filtered = _ok(client.get(path, params={"limit": 0, "label": "eq.Row 0"}, headers={"Prefer": "count=exact"}))
    assert filtered["total"] == 1


def test_new_index_preserves_row_update_cas_and_cleanup(table_client):
    client, base, _, _ = table_client
    path = base + "/records/rows"
    added = _ok(client.post(path, json={"label": "Temporary"}, headers={"Prefer": "return=representation"}), 201)["items"][0]
    query = {"id": "eq." + added["id"], "updated_at": "eq." + added["updated_at"]}
    changed = _ok(client.patch(path, params=query, json={"label": "Changed"}, headers={"Prefer": "return=representation"}))
    assert changed["items"][0]["label"] == "Changed"
    stale = _ok(client.patch(path, params=query, json={"label": "Must not win"}, headers={"Prefer": "return=representation"}))
    assert stale["items"] == []  # Interactive caller treats this as a CAS conflict.
    _ok(client.delete(path, params={"id": "eq." + added["id"]}, headers={"Prefer": "return=representation"}))
    count = _ok(client.get(path, params={"limit": 0}, headers={"Prefer": "count=exact"}))
    assert count["total"] == 6


def test_schema_only_and_count_only_do_not_weaken_acl(table_client):
    client, base, vault, _ = table_client
    reader, outsider = os.getenv("AKB_E2E_READER_PAT"), os.getenv("AKB_E2E_OUTSIDER_PAT")
    if not reader or not outsider:
        pytest.skip("supply reader and outsider fixture PATs for ACL checks")
    reader_headers = {"Authorization": "Bearer " + reader}
    principal = _ok(client.get("/api/v1/auth/me", headers=reader_headers))
    _ok(client.post(f"/api/v1/vaults/{vault}/grant", json={"user": principal["username"], "role": "reader"}))
    for headers, allowed in [(reader_headers, True), ({"Authorization": "Bearer " + outsider}, False)]:
        for path, params in [(base, {"include_row_counts": "false"}),
                             (base + "/records/rows", {"limit": 0})]:
            response = client.get(path, params=params, headers={**headers, "Prefer": "count=exact"})
            if allowed:
                _ok(response)
            else:
                assert response.status_code in (403, 404)
    assert client.post(base + "/records/rows", json={"label": "Denied"}, headers=reader_headers).status_code == 403
