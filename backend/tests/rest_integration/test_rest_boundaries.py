"""Status and response contracts for ordinary PAT-authenticated REST routes."""

import httpx
import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services


async def test_pat_can_update_profile(api: httpx.AsyncClient) -> None:
    actor = await register_user(api, label="profile-boundary")
    assert actor.pat.startswith("akb_")
    profile = await api.patch(
        "/api/v1/auth/me",
        headers=actor.headers,
        json={"display_name": "Boundary User"},
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["updated"] is True


async def test_table_catalog_exposes_sql_name(api: httpx.AsyncClient) -> None:
    actor = await register_user(api, label="table-boundary")
    vault = await api.post(
        "/api/v1/vaults", params={"name": "table-boundary"}, headers=actor.headers
    )
    assert vault.status_code == 200, vault.text
    assert vault.json()["vault_id"]

    path = "/api/v1/tables/table-boundary"
    table = await api.post(
        path,
        headers=actor.headers,
        json={"name": "items", "columns": [{"name": "product", "type": "text"}]},
    )
    assert table.status_code == 200, table.text
    assert table.json()["uri"]

    catalog = await api.get(path, headers=actor.headers)
    assert catalog.status_code == 200, catalog.text
    (listed,) = catalog.json()["items"]
    assert listed["name"] == "items"
    assert listed["sql_name"] == "items"


async def test_unpublishing_revokes_anonymous_document_access(api: httpx.AsyncClient) -> None:
    actor = await register_user(api, label="publication-boundary")
    vault_name = "publication-boundary"
    vault = await api.post(
        "/api/v1/vaults", params={"name": vault_name}, headers=actor.headers
    )
    assert vault.status_code == 200, vault.text

    doc = await api.post(
        "/api/v1/documents",
        headers=actor.headers,
        json={
            "vault": vault_name,
            "collection": "specs",
            "title": "REST Boundary Document",
            "content": "## Public\n\nREST boundary check.",
            "type": "note",
            "tags": [],
        },
    )
    assert doc.status_code == 200, doc.text
    uri = doc.json()["uri"]
    assert uri

    path = f"/api/v1/publications/{vault_name}"
    published = await api.post(f"{path}/create", headers=actor.headers, json={"uri": uri})
    assert published.status_code == 200, published.text
    slug = published.json()["slug"]
    assert slug

    public_path = f"/api/v1/public/{slug}"
    opened = await api.get(public_path)
    assert opened.status_code == 200, opened.text
    assert opened.json()["title"] == "REST Boundary Document"

    deleted = await api.delete(f"{path}/{slug}", headers=actor.headers)
    assert deleted.status_code == 200, deleted.text
    opened = await api.get(public_path)
    assert opened.status_code == 404, opened.text
