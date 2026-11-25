import httpx
import pytest

from tests.rest_integration.helpers import Actor, register_user

pytestmark = pytest.mark.live_services


@pytest.fixture
async def collection_vault(api: httpx.AsyncClient):
    owner = await register_user(api, label="collection-owner")
    vault = f"collection-life-{owner.id.hex[:12]}"
    response = await api.post("/api/v1/vaults", headers=owner.headers, params={"name": vault})
    assert response.raise_for_status().json()["vault_id"]

    yield owner, vault

    response = await api.delete(f"/api/v1/vaults/{vault}", headers=owner.headers)
    assert response.raise_for_status().json()["deleted"] is True


async def test_collection_create_and_delete_envelopes(api: httpx.AsyncClient, collection_vault: tuple[Actor, str]):
    owner, vault = collection_vault
    collections = f"/api/v1/collections/{vault}"
    collection = "rest-contract"

    for summary, created in ((None, True), ("ignored", False)):
        response = await api.post(collections, headers=owner.headers, json={"path": collection, "summary": summary})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["kind"] == "collection_create"
        assert result["ok"] is True
        assert result["created"] is created
        assert result["collection"]["summary"] is None
        assert result["collection"]["doc_count"] == 0

    response = await api.post(
        "/api/v1/documents",
        headers=owner.headers,
        json={"vault": vault, "collection": collection, "title": "RestContractDoc", "content": "## body"},
    )
    assert response.raise_for_status().json()["uri"]

    response = await api.delete(f"{collections}/{collection}", headers=owner.headers)
    assert response.status_code == 409, response.text
    error = response.json()
    assert error["code"] == "conflict"
    for key, count in {"doc_count": 1, "file_count": 0, "sub_collection_count": 0, "table_count": 0}.items():
        assert error["details"][key] == count
    assert error["detail"]["file_count"] == 0

    response = await api.delete(f"{collections}/{collection}", headers=owner.headers, params={"recursive": True})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["kind"] == "collection_delete"
    assert result["ok"] is True
    assert result["collection"] == collection
    assert result["deleted_docs"] == 1
    assert result["deleted_files"] == 0
    assert result["deleted_sub_collections"] == 0
    assert result["deleted_tables"] == 0
    response = await api.delete(f"{collections}/{collection}", headers=owner.headers)
    assert response.status_code == 404, response.text


async def test_collection_and_table_deletion_permissions(api: httpx.AsyncClient, collection_vault: tuple[Actor, str]):
    owner, vault = collection_vault
    member = await register_user(api, label="collection-member")
    collections = f"/api/v1/collections/{vault}"
    tables = f"/api/v1/tables/{vault}"
    response = await api.post(collections, headers=owner.headers, json={"path": "keepempty"})
    response.raise_for_status()

    response = await api.post(collections, headers=member.headers, json={"path": "unauthorized"})
    assert response.status_code == 403, response.text
    response = await api.post(
        f"/api/v1/vaults/{vault}/grant", headers=owner.headers, json={"user": member.username, "role": "reader"}
    )
    assert response.raise_for_status().json()["granted"] is True
    response = await api.post(collections, headers=member.headers, json={"path": "reader-tried"})
    assert response.status_code == 403, response.text
    response = await api.delete(f"{collections}/keepempty", headers=member.headers)
    assert response.status_code == 403, response.text

    response = await api.post(
        f"/api/v1/vaults/{vault}/grant", headers=owner.headers, json={"user": member.username, "role": "writer"}
    )
    assert response.raise_for_status().json()["granted"] is True
    table = "writer_guard_table"
    response = await api.post(
        tables,
        headers=owner.headers,
        json={"name": table, "collection": "writer-guard", "columns": [{"name": "value", "type": "text"}]},
    )
    assert response.raise_for_status().json()["name"] == table

    response = await api.delete(f"{tables}/{table}", headers=member.headers)
    assert response.status_code == 403, response.text
    response = await api.delete(f"{collections}/writer-guard", headers=member.headers, params={"recursive": True})
    assert response.status_code == 403, response.text
    response = await api.get(tables, headers=owner.headers)
    assert any(item["name"] == table for item in response.raise_for_status().json()["items"])

    response = await api.delete(f"{collections}/writer-guard", headers=owner.headers, params={"recursive": True})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_tables"] == 1


async def test_delete_synthesized_parent_collection(api: httpx.AsyncClient, collection_vault: tuple[Actor, str]):
    owner, vault = collection_vault
    collections = f"/api/v1/collections/{vault}"
    response = await api.post(collections, headers=owner.headers, json={"path": "nested/inner"})
    assert response.raise_for_status().json()["ok"] is True

    response = await api.delete(f"{collections}/nested", headers=owner.headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["sub_collection_count"] >= 1
    response = await api.delete(f"{collections}/nested", headers=owner.headers, params={"recursive": True})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_sub_collections"] >= 1
    response = await api.delete(f"{collections}/totally-absent", headers=owner.headers)
    assert response.status_code == 404, response.text
