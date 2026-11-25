import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services


async def test_bulk_insert_patch_upsert_and_write_ast_delete(api):
    owner = await register_user(api, label="row-write")
    headers = owner.headers
    representation = {**headers, "Prefer": "return=representation"}
    vault = "row-write-vault"
    tables = f"/api/v1/tables/{vault}"
    rows = f"{tables}/incidents/rows"

    response = await api.post(
        "/api/v1/vaults", headers=headers, params={"name": vault, "description": "row-write-e2e"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["vault_id"]

    response = await api.post(tables, headers=headers, json={
        "name": "incidents",
        "columns": [
            {"name": "external_id", "type": "text", "required": True},
            {"name": "title", "type": "text"},
            {"name": "severity", "type": "text"},
            {"name": "metadata", "type": "json"},
        ],
        "unique_keys": [{"columns": ["external_id"]}],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "incidents"
    assert body["unique_keys"]

    response = await api.post(rows, headers=representation, json=[
        {"external_id": "INC-1", "title": "A", "severity": "high", "created_by": "mallory"},
        {"external_id": "INC-2", "title": "B", "metadata": {"tier": "gold"}},
    ])
    assert response.status_code == 201, response.text
    assert response.headers["Content-Range"] == "0-1/2"
    body = response.json()
    assert body["kind"] == "table_query"
    assert body["total"] == 2
    assert [row["external_id"] for row in body["items"]] == ["INC-1", "INC-2"]
    assert all(row["created_by"] == owner.username for row in body["items"])

    response = await api.patch(
        rows, headers=headers, params={"order": "id.asc"}, json={"severity": "critical"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "unfiltered_mutation"

    response = await api.patch(
        rows, headers=representation,
        params={"external_id": "eq.INC-1", "select": "external_id,severity,created_by"},
        json={"severity": "critical", "created_by": "mallory", "updated_at": "2099-01-01T00:00:00Z"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["Content-Range"] == "0-0/1"
    assert response.json()["items"] == [{
        "external_id": "INC-1", "severity": "critical", "created_by": owner.username,
    }]

    response = await api.post(
        rows, headers=representation,
        params={"on_conflict": "external_id", "select": "external_id,title,severity"},
        json={"external_id": "INC-1", "title": "A merged", "severity": "medium"},
    )
    assert response.is_success, response.text
    assert response.json()["items"] == [{"external_id": "INC-1", "title": "A merged", "severity": "medium"}]

    response = await api.post(
        rows, headers=headers, params={"on_conflict": "severity"},
        json={"external_id": "INC-3", "severity": "low"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "no_unique_constraint"

    response = await api.post(f"{tables}/incidents/query", headers=headers, json={
        "delete": True,
        "where": {"col": "external_id", "op": "eq", "val": "INC-2"},
        "returning": ["external_id"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [{"external_id": "INC-2"}]

    response = await api.get(
        rows, headers=headers, params={"select": "external_id,title,severity", "order": "external_id.asc"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"] == [{"external_id": "INC-1", "title": "A merged", "severity": "medium"}]
