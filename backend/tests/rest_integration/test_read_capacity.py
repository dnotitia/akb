"""Count, pagination, concurrency, and ACL contracts over the table API."""

import uuid

import pytest

from .helpers import register_user

pytestmark = pytest.mark.live_services


def _ok(response, status=200):
    assert response.status_code == status, f"HTTP {response.status_code}, expected {status}"
    return response.json()


@pytest.fixture
async def table_setup(api):
    owner = await register_user(api, label="read-capacity-owner")
    headers = owner.headers
    vault = "read-capacity-test-" + uuid.uuid4().hex[:12]
    assert _ok(await api.get("/api/v1/auth/me", headers=headers))["is_admin"] is False
    _ok(await api.post("/api/v1/vaults", params={"name": vault}, headers=headers))
    try:
        base = f"/api/v1/tables/{vault}"
        _ok(await api.post(base, headers=headers, json={
            "name": "records", "columns": [{"name": "label", "type": "text"}],
        }))
        assert _ok(await api.get(base + "/records/schema", headers=headers))["drift"]["has_drift"] is False
        _ok(await api.post(
            base + "/records/rows",
            json=[{"label": f"Row {i}"} for i in range(6)],
            headers={**headers, "Prefer": "return=representation"},
        ), 201)
        yield base, vault, headers
    finally:
        _ok(await api.delete("/api/v1/vaults/" + vault, headers=headers))


@pytest.mark.parametrize("counted", [True, False])
async def test_catalog_has_exact_default_and_explicit_unmeasured_counts(api, table_setup, counted):
    base, _, headers = table_setup
    catalog = _ok(await api.get(
        base, params={} if counted else {"include_row_counts": "false"}, headers=headers,
    ))
    assert catalog["total"] == 1
    item = catalog["items"][0]
    assert item["name"] == "records" and item["columns"][0]["name"] == "label"
    if counted:
        assert item["row_count"] == 6
    else:
        assert "row_count" not in item


@pytest.mark.parametrize("exact", [True, False])
async def test_page_contract_and_count_only_are_distinct(api, table_setup, exact):
    base, _, headers = table_setup
    path = base + "/records/rows"
    page_headers = {**headers, "Prefer": "count=exact"} if exact else headers
    query = {"limit": 3, "order": "created_at.desc,id.desc"}
    first = _ok(await api.get(path, params=query, headers=page_headers))
    second = _ok(await api.get(path, params={**query, "offset": 3}, headers=page_headers))
    assert first["total"] == second["total"] == (6 if exact else 3)
    assert len({row["id"] for row in first["items"] + second["items"]}) == 6
    # Sentinel uses the same offset stride as visible rows, not limit+1.
    sentinel = _ok(await api.get(path, params={**query, "limit": 4}, headers=headers))
    assert sentinel["items"][:3] == first["items"]
    assert sentinel["items"][3] == second["items"][0]
    count_headers = {**headers, "Prefer": "count=exact"}
    counted = _ok(await api.get(path, params={"limit": 0}, headers=count_headers))
    assert counted["total"] == 6 and counted["items"] == []
    filtered = _ok(await api.get(path, params={"limit": 0, "label": "eq.Row 0"}, headers=count_headers))
    assert filtered["total"] == 1


async def test_new_index_preserves_row_update_cas_and_cleanup(api, table_setup):
    base, _, headers = table_setup
    path = base + "/records/rows"
    write_headers = {**headers, "Prefer": "return=representation"}
    added = _ok(await api.post(path, json={"label": "Temporary"}, headers=write_headers), 201)["items"][0]
    query = {"id": "eq." + added["id"], "updated_at": "eq." + added["updated_at"]}
    changed = _ok(await api.patch(path, params=query, json={"label": "Changed"}, headers=write_headers))
    assert changed["items"][0]["label"] == "Changed"
    stale = _ok(await api.patch(path, params=query, json={"label": "Must not win"}, headers=write_headers))
    assert stale["items"] == []  # Interactive caller treats this as a CAS conflict.
    _ok(await api.delete(path, params={"id": "eq." + added["id"]}, headers=write_headers))
    count = _ok(await api.get(path, params={"limit": 0}, headers={**headers, "Prefer": "count=exact"}))
    assert count["total"] == 6


async def test_schema_only_and_count_only_do_not_weaken_acl(api, table_setup):
    base, vault, owner_headers = table_setup
    reader = await register_user(api, label="read-capacity-reader")
    outsider = await register_user(api, label="read-capacity-outsider")
    principal = _ok(await api.get("/api/v1/auth/me", headers=reader.headers))
    _ok(await api.post(f"/api/v1/vaults/{vault}/grant", headers=owner_headers, json={
        "user": principal["username"], "role": "reader",
    }))
    for actor, allowed in [(reader, True), (outsider, False)]:
        for path, params in [(base, {"include_row_counts": "false"}),
                             (base + "/records/rows", {"limit": 0})]:
            response = await api.get(path, params=params, headers={**actor.headers, "Prefer": "count=exact"})
            if allowed:
                _ok(response)
            else:
                assert response.status_code in (403, 404)
    response = await api.post(base + "/records/rows", json={"label": "Denied"}, headers=reader.headers)
    assert response.status_code == 403
    assert _ok(await api.get(base + "/records/rows", params={"limit": 0},
                            headers={**owner_headers, "Prefer": "count=exact"}))["total"] == 6
