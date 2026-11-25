"""REST and MCP graph catalog behavior against the same PostgreSQL database."""

import uuid

import pytest

from .helpers import register_user
from .mcp_client import call_json, mcp_session

pytestmark = pytest.mark.live_services


@pytest.fixture
async def graph_installation(api, pg_pool):
    owner = await register_user(api, label="graph-owner")
    await pg_pool.execute("UPDATE users SET is_admin = TRUE WHERE id = $1", owner.id)
    headers = owner.headers
    vault = "graph-catalog-test-" + uuid.uuid4().hex[:12]
    created = await api.post("/api/v1/vaults", params={"name": vault}, headers=headers)
    assert created.status_code == 200, created.text
    try:
        seed = await api.get(f"/api/v1/documents/{vault}/overview/vault-skill.md", headers=headers)
        assert seed.status_code in (200, 404)
        docs = []
        for slug, status in (("alpha", "active"), ("beta", "active"), ("retired", "archived")):
            written = await api.post("/api/v1/documents", headers=headers, json={
                "vault": vault, "collection": "qa", "slug": slug,
                "title": "Title " + slug, "content": "Graph catalog canary " + slug,
                "status": status,
            })
            assert written.status_code == 200, written.text
            read = await api.get(f"/api/v1/documents/{vault}/qa/{slug}.md", headers=headers)
            assert read.status_code == 200, read.text
            assert read.json()["uri"] == written.json()["uri"]
            docs.append(read.json())
        # Legacy create may seed a guide. Discover it through the document API.
        if seed.status_code == 200:
            docs.append(seed.json())
        yield owner, vault, docs
    finally:
        deleted = await api.delete(f"/api/v1/vaults/{vault}", headers=headers)
        assert deleted.status_code == 200, deleted.text


async def _get(api, headers, path, **params):
    response = await api.get("/api/v1/" + path, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_provenance_matches_the_document_that_was_read(api, graph_installation):
    owner, _, docs = graph_installation
    doc = docs[0]
    result = await _get(api, owner.headers, "provenance", uri=doc["uri"])
    uuid.UUID(result["doc_id"])
    for field in ("uri", "path", "title", "vault", "current_commit", "created_by"):
        assert result[field] == doc[field]
    assert result["relations"] == []


async def test_unlinked_live_documents_are_orphans_not_archived_documents(api, graph_installation):
    owner, vault, docs = graph_installation
    active = [doc for doc in docs if doc["status"] != "archived"]
    expected = {doc["uri"] for doc in active}
    overview = await _get(api, owner.headers, "graph/overview", vault=vault, top_k=1000)
    assert {node["uri"] for node in overview["nodes"]} == expected
    assert {node["name"] for node in overview["nodes"]} == {doc["title"] for doc in active}
    assert overview["orphans_returned"] == len(active)
    assert overview["nodes_total"] == overview["returned"] == overview["edges_total"] == 0
    assert overview["orphans_truncated"] is False
    health = await _get(api, owner.headers, "graph/health", vault=vault, limit=200)
    assert health["orphans"]["count"] == len(active)
    assert {node["uri"] for node in health["orphans"]["sample"]} == expected


async def test_link_unlink_move_and_delete_keep_current_orphan_identity(api, graph_installation):
    owner, vault, docs = graph_installation
    headers = owner.headers
    source, target = docs[:2]
    extra = sum(doc["status"] != "archived" for doc in docs[3:])
    relation = {"source": source["uri"], "target": target["uri"], "relation": "related_to"}
    linked = await api.post("/api/v1/relations", json=relation, headers=headers)
    assert linked.status_code == 200, linked.text
    overview = await _get(api, headers, "graph/overview", vault=vault)
    assert overview["orphans_returned"] == extra
    assert len(overview["nodes"]) == 2 + extra
    assert overview["nodes_total"] == 2
    connected = [node for node in overview["nodes"] if node["degree"] > 0]
    assert {node["name"] for node in connected} == {source["title"], target["title"]}
    assert (await _get(api, headers, "graph/health", vault=vault))["orphans"]["count"] == extra
    unlinked = await api.delete("/api/v1/relations", params=relation, headers=headers)
    assert unlinked.status_code == 200, unlinked.text
    assert (await _get(api, headers, "graph/health", vault=vault))["orphans"]["count"] == 2 + extra

    before = await _get(api, headers, "provenance", uri=source["uri"])
    moved = await api.post(
        f"/api/v1/documents/{vault}/{source['path']}/move", json={"slug": "renamed"}, headers=headers,
    )
    assert moved.status_code == 200, moved.text
    after = await _get(api, headers, "provenance", uri=moved.json()["uri"])
    assert after["doc_id"] == before["doc_id"]
    assert after["path"] != before["path"]
    deleted = await api.delete(f"/api/v1/documents/{vault}/{after['path']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    missing = await api.get("/api/v1/provenance", params={"uri": after["uri"]}, headers=headers)
    assert missing.status_code == 404, missing.text
    assert (await _get(api, headers, "graph/health", vault=vault))["orphans"]["count"] == 1 + extra


async def test_mcp_provenance_and_orphans_use_the_same_catalog(asgi_app, graph_installation):
    owner, vault, docs = graph_installation
    async with mcp_session(asgi_app, owner.pat) as client:
        provenance = await call_json(client, "akb_provenance", {"uri": docs[0]["uri"]})
        assert "error" not in provenance, provenance
        assert provenance["uri"] == docs[0]["uri"]
        assert provenance["current_commit"] == docs[0]["current_commit"]
        graph = await call_json(client, "akb_graph", {"vault": vault})
        assert "error" not in graph, graph
        assert {node["uri"] for node in graph["nodes"]} == {
            doc["uri"] for doc in docs if doc["status"] != "archived"
        }


async def test_reader_and_outsider_boundaries_are_preserved(api, graph_installation):
    owner, vault, docs = graph_installation
    reader = await register_user(api, label="graph-reader")
    outsider = await register_user(api, label="graph-outsider")
    profile = await api.get("/api/v1/auth/me", headers=reader.headers)
    assert profile.status_code == 200, profile.text
    reader_id = profile.json()["user_id"]
    granted = await api.post(
        f"/api/v1/vaults/{vault}/grant", json={"user_id": reader_id, "role": "reader"}, headers=owner.headers,
    )
    assert granted.status_code == 200, granted.text
    paths = [
        ("provenance", {"uri": docs[0]["uri"]}),
        ("graph/overview", {"vault": vault}),
        ("graph/health", {"vault": vault}),
    ]
    for actor, status in ((reader, 200), (outsider, 403)):
        for path, params in paths:
            response = await api.get("/api/v1/" + path, params=params, headers=actor.headers)
            assert response.status_code == status, response.text
