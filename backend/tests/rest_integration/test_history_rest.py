import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services


async def test_history_openapi_contract(api: httpx.AsyncClient):
    response = await api.get("/openapi.json")
    document = response.raise_for_status().json()
    operations = {
        "/api/v1/activity/{vault}": ("activityList", "activity", "AkbActivityEnvelope", "activity"),
        "/api/v1/recent": ("activityRecent", "activity", "AkbRecentChangesEnvelope", "recent_changes"),
        "/api/v1/history/{vault}/{doc_id}": (
            "documentsHistory",
            "documents",
            "AkbDocumentHistoryEnvelope",
            "document_history",
        ),
        "/api/v1/diff/{vault}/{doc_id}": ("documentsDiff", "documents", "AkbDocumentDiffEnvelope", "document_diff"),
    }
    schemas = document["components"]["schemas"]
    union = schemas["AkbSuccessEnvelope"]
    operation_ids = [
        operation.get("operationId")
        for path in document["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]

    for path, (operation_id, tag, model, kind) in operations.items():
        operation = document["paths"][path]["get"]
        assert operation["operationId"] == operation_id
        assert operation_ids.count(operation_id) == 1
        assert operation["tags"] == [tag]
        ref = {"$ref": f"#/components/schemas/{model}"}
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == ref
        for status in ("400", "401", "403", "404", "409", "422", "500"):
            assert operation["responses"][status]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/AkbError"
            }
        leaf = schemas[model]
        assert "kind" in leaf["required"]
        assert leaf["properties"]["kind"]["enum"] == [kind]
        assert sum(entry.get("$ref") == ref["$ref"] for entry in union["oneOf"]) == 1
        assert union["discriminator"]["mapping"][kind] == ref["$ref"]

    assert schemas["ActivityEntry"]["properties"]["files"]["items"]["$ref"].endswith("ActivityFileChange")
    assert schemas["DocumentHistoryEntry"]["properties"]["date"]["format"] == "date-time"
    assert schemas["AkbDocumentDiffEnvelope"]["properties"]["diff"]["type"] == "string"


@pytest.fixture
async def revised_document(api: httpx.AsyncClient):
    owner = await register_user(api, label="history-owner")
    vault = f"history-{owner.id.hex[:12]}"
    response = await api.post("/api/v1/vaults", headers=owner.headers, params={"name": vault})
    response.raise_for_status()

    document = {
        "vault": vault,
        "collection": "history-test",
        "title": "History Doc",
        "content": "## V1",
        "slug": "hist-doc",
    }
    response = await api.post("/api/v1/documents", headers=owner.headers, json=document)
    created = response.raise_for_status().json()
    assert created["path"] and created["current_commit"]
    response = await api.patch(
        f"/api/v1/documents/{vault}/{created['path']}",
        headers=owner.headers,
        json={
            "content": "## V2 updated",
            "expected_commit": created["current_commit"],
            "message": "history REST update",
        },
    )
    updated = response.raise_for_status().json()
    assert updated["current_commit"] and updated["current_commit"] != created["current_commit"]
    return owner, document, updated


async def test_history_activity_diff_and_recreated_document_lineage(api: httpx.AsyncClient, revised_document):
    owner, original, updated = revised_document
    vault = original["vault"]
    path = updated["path"]
    history_path = f"/api/v1/history/{vault}/{path}"
    activity_path = f"/api/v1/activity/{vault}"
    diff_path = f"/api/v1/diff/{vault}/{path}"

    response = await api.get(history_path, headers=owner.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "document_history"
    history = body["history"]
    assert len(history) >= 2
    assert history[0]["hash"] and updated["current_commit"].startswith(history[0]["hash"])
    assert history[0]["date"]
    assert history[0]["author"] == owner.username
    assert history[0]["author_name"] == owner.username
    assert all("author_name" in entry for entry in history)

    response = await api.get(history_path, headers=owner.headers, params={"limit": 1})
    assert len(response.raise_for_status().json()["history"]) == 1
    for limit in (0, 500):
        response = await api.get(history_path, headers=owner.headers, params={"limit": limit})
        assert response.status_code == 422, response.text

    response = await api.get(activity_path, headers=owner.headers)
    activity = response.raise_for_status().json()
    assert activity["kind"] == "activity"
    assert len(activity["activity"]) >= 2
    response = await api.get(activity_path, headers=owner.headers, params={"limit": 1})
    assert len(response.raise_for_status().json()["activity"]) == 1
    for filters in ({"collection": original["collection"]}, {"author": owner.username}):
        response = await api.get(activity_path, headers=owner.headers, params=filters)
        assert len(response.raise_for_status().json()["activity"]) >= 2
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
    response = await api.get(activity_path, headers=owner.headers, params={"since": tomorrow})
    assert response.raise_for_status().json()["activity"] == []

    response = await api.get(diff_path, headers=owner.headers, params={"commit": history[0]["hash"]})
    diff = response.raise_for_status().json()
    assert diff["kind"] == "document_diff"
    assert diff["type"] == "modified"
    assert "error" not in diff
    response = await api.get(diff_path, headers=owner.headers, params={"commit": "not-a-commit"})
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "unknown"
    assert response.json()["error"] == "commit not found"

    response = await api.delete(f"/api/v1/documents/{vault}/{path}", headers=owner.headers)
    response.raise_for_status()
    # Bare Git commit dates have second precision. The delete must be older than the new lineage.
    await asyncio.sleep(1)
    response = await api.post("/api/v1/documents", headers=owner.headers, json={**original, "content": "## reborn"})
    recreated = response.raise_for_status().json()
    assert recreated["path"] == path
    response = await api.get(history_path, headers=owner.headers)
    (reborn,) = response.raise_for_status().json()["history"]
    assert reborn["hash"] and recreated["current_commit"].startswith(reborn["hash"])


async def test_history_access_and_recent_visibility(api: httpx.AsyncClient, revised_document):
    owner, original, updated = revised_document
    vault = original["vault"]
    path = updated["path"]
    commit = {"commit": updated["current_commit"]}
    reader = await register_user(api, label="history-reader")
    outsider = await register_user(api, label="history-outsider")
    response = await api.post(
        f"/api/v1/vaults/{vault}/grant", headers=owner.headers, json={"user": reader.username, "role": "reader"}
    )
    assert response.raise_for_status().json()["granted"] is True

    for route, query in (
        (f"/api/v1/history/{vault}/{path}", {}),
        (f"/api/v1/activity/{vault}", {}),
        (f"/api/v1/diff/{vault}/{path}", commit),
    ):
        for headers, status in ((reader.headers, 200), (outsider.headers, 403), ({}, 401)):
            response = await api.get(route, headers=headers, params=query)
            assert response.status_code == status, response.text

    for route, query in (
        (f"/api/v1/history/{vault}/history-test/does-not-exist.md", {}),
        (f"/api/v1/history/ghost-{vault}/{path}", {}),
        (f"/api/v1/diff/{vault}/history-test/does-not-exist.md", commit),
        (f"/api/v1/activity/ghost-{vault}", {}),
    ):
        response = await api.get(route, headers=owner.headers, params=query)
        assert response.status_code == 404, response.text

    for actor in (owner, reader):
        response = await api.get("/api/v1/recent", headers=actor.headers, params={"vault": vault})
        recent = response.raise_for_status().json()
        assert recent["kind"] == "recent_changes"
        assert any(change["vault"] == vault for change in recent["changes"])

    response = await api.get("/api/v1/recent", headers=outsider.headers)
    assert all(change["vault"] != vault for change in response.raise_for_status().json()["changes"])
    response = await api.get("/api/v1/recent", headers=outsider.headers, params={"vault": vault})
    assert response.status_code == 403, response.text

    response = await api.patch(f"/api/v1/vaults/{vault}", headers=owner.headers, json={"public_access": "reader"})
    response.raise_for_status()
    response = await api.get("/api/v1/recent", headers=outsider.headers)
    assert any(change["vault"] == vault for change in response.raise_for_status().json()["changes"])
