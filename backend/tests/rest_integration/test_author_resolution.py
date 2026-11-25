import httpx
import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services


async def test_actor_username_resolves_to_display_name_on_each_read_surface(api: httpx.AsyncClient):
    owner = await register_user(api, label="author-resolution")
    vault = f"author-resolution-{owner.id.hex[:12]}"
    display_name = "Display Name E2E"
    assert display_name != owner.username

    response = await api.patch("/api/v1/auth/me", headers=owner.headers, json={"display_name": display_name})
    assert response.raise_for_status().json()["display_name"] == display_name
    response = await api.post("/api/v1/vaults", headers=owner.headers, params={"name": vault})
    response.raise_for_status()

    response = await api.post(
        "/api/v1/documents",
        headers=owner.headers,
        json={"vault": vault, "collection": "authres", "title": "Author Doc", "content": "# Hi", "slug": "author-doc"},
    )
    document = response.raise_for_status().json()
    doc_path = document["path"]
    doc_uri = document["uri"]
    assert doc_path and doc_uri

    response = await api.get(f"/api/v1/documents/{vault}/{doc_path}", headers=owner.headers)
    assert response.raise_for_status().json()["created_by_name"] == display_name
    response = await api.get(f"/api/v1/activity/{vault}", headers=owner.headers)
    assert response.raise_for_status().json()["activity"][0]["author_name"] == display_name
    response = await api.get(f"/api/v1/history/{vault}/{doc_path}", headers=owner.headers)
    assert response.raise_for_status().json()["history"][0]["author_name"] == display_name

    response = await api.post(
        f"/api/v1/publications/{vault}/create",
        headers=owner.headers,
        json={"resource_type": "document", "uri": doc_uri},
    )
    slug = response.raise_for_status().json()["slug"]
    assert slug
    response = await api.get(f"/api/v1/public/{slug}")
    assert response.raise_for_status().json()["created_by_name"] == display_name
