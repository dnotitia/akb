"""Opt-in REST/MCP graph checks on API-created documents in either backend.

Set AKB_GRAPH_TEST_URL and AKB_E2E_PAT or fixture login credentials.
AKB_E2E_CA_FILE supplies a private CA. Optional AKB_E2E_OUTSIDER_PAT and
AKB_E2E_READER_PAT / AKB_E2E_READER_ID exercise actual membership boundaries.
Only this module's randomly named Vaults are deleted.
"""

from __future__ import annotations

import json
import os
import ssl
import uuid

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


@pytest.fixture
def graph_installation():
    origin = os.getenv("AKB_GRAPH_TEST_URL")
    if not origin:
        pytest.skip("set AKB_GRAPH_TEST_URL for actual REST/MCP graph checks")
    ca_file = os.getenv("AKB_E2E_CA_FILE")
    verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
    with httpx.Client(base_url=origin, verify=verify, timeout=30) as client:
        token = os.getenv("AKB_E2E_PAT")
        if not token:
            username, password = os.getenv("AKB_E2E_USERNAME"), os.getenv("AKB_E2E_PASSWORD")
            if not username or not password:
                pytest.fail("provide a fixture PAT or fixture login credentials")
            login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
            assert login.status_code == 200
            token = login.json()["token"]
        client.headers["Authorization"] = "Bearer " + token
        vault = "graph-catalog-test-" + uuid.uuid4().hex[:12]
        created = client.post("/api/v1/vaults", params={"name": vault})
        assert created.status_code == 200, created.text
        minted = None
        try:
            if not os.getenv("AKB_E2E_PAT"):
                # MCP uses a PAT, not the browser session token obtained at
                # login. Keep that distinction in both test environments.
                issued = client.post("/api/v1/auth/tokens", json={
                    "name": "graph-catalog-http", "expires_days": 1, "scopes": ["read", "write"],
                })
                assert issued.status_code == 200
                token = issued.json()["token"]
                minted = issued.json()["token_id"]
            seed = client.get(f"/api/v1/documents/{vault}/overview/vault-skill.md")
            assert seed.status_code in (200, 404)
            docs = []
            for slug, status in (("alpha", "active"), ("beta", "active"), ("retired", "archived")):
                written = client.post("/api/v1/documents", json={
                    "vault": vault, "collection": "qa", "slug": slug,
                    "title": "Title " + slug, "content": "Graph catalog canary " + slug,
                    "status": status,
                })
                assert written.status_code == 200, written.text
                read = client.get(f"/api/v1/documents/{vault}/qa/{slug}.md")
                assert read.status_code == 200, read.text
                assert read.json()["uri"] == written.json()["uri"]
                docs.append(read.json())
            # Legacy create may seed a guide. Establish it through the
            # independent document API, never the graph being tested.
            if seed.status_code == 200:
                docs.append(seed.json())
            yield client, vault, docs, origin, token, verify
        finally:
            deleted = client.delete("/api/v1/vaults/" + vault)
            revoked = client.delete("/api/v1/auth/tokens/" + minted) if minted else None
            assert deleted.status_code == 200, deleted.text
            if revoked is not None:
                assert revoked.status_code == 200


def _get(client, path, **params):
    response = client.get("/api/v1/" + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_provenance_matches_the_document_that_was_read(graph_installation):
    client, _vault, docs, *_ = graph_installation
    doc = docs[0]
    result = _get(client, "provenance", uri=doc["uri"])
    uuid.UUID(result["doc_id"])
    for field in ("uri", "path", "title", "vault", "current_commit", "created_by"):
        assert result[field] == doc[field]
    assert result["relations"] == []


def test_unlinked_live_documents_are_orphans_not_archived_documents(graph_installation):
    client, vault, docs, *_ = graph_installation
    active = [doc for doc in docs if doc["status"] != "archived"]
    expected = {doc["uri"] for doc in active}
    overview = _get(client, "graph/overview", vault=vault, top_k=1000)
    assert {node["uri"] for node in overview["nodes"]} == expected
    assert {node["name"] for node in overview["nodes"]} == {doc["title"] for doc in active}
    assert overview["orphans_returned"] == len(active)
    assert overview["nodes_total"] == overview["returned"] == overview["edges_total"] == 0
    assert overview["orphans_truncated"] is False
    health = _get(client, "graph/health", vault=vault, limit=200)
    assert health["orphans"]["count"] == len(active)
    assert {node["uri"] for node in health["orphans"]["sample"]} == expected


def test_link_unlink_move_and_delete_keep_current_orphan_identity(graph_installation):
    client, vault, docs, *_ = graph_installation
    source, target = docs[:2]
    extra = sum(doc["status"] != "archived" for doc in docs[3:])
    relation = {"source": source["uri"], "target": target["uri"], "relation": "related_to"}
    linked = client.post("/api/v1/relations", json=relation)
    assert linked.status_code == 200, linked.text
    overview = _get(client, "graph/overview", vault=vault)
    assert overview["orphans_returned"] == extra
    assert len(overview["nodes"]) == 2 + extra
    assert overview["nodes_total"] == 2
    connected = [node for node in overview["nodes"] if node["degree"] > 0]
    assert {node["name"] for node in connected} == {source["title"], target["title"]}
    assert _get(client, "graph/health", vault=vault)["orphans"]["count"] == extra
    unlinked = client.delete("/api/v1/relations", params=relation)
    assert unlinked.status_code == 200, unlinked.text
    assert _get(client, "graph/health", vault=vault)["orphans"]["count"] == 2 + extra

    before = _get(client, "provenance", uri=source["uri"])
    moved = client.post(f"/api/v1/documents/{vault}/{source['path']}/move", json={"slug": "renamed"})
    assert moved.status_code == 200, moved.text
    after = _get(client, "provenance", uri=moved.json()["uri"])
    assert after["doc_id"] == before["doc_id"]
    assert after["path"] != before["path"]
    deleted = client.delete(f"/api/v1/documents/{vault}/{after['path']}")
    assert deleted.status_code == 200, deleted.text
    missing = client.get("/api/v1/provenance", params={"uri": after["uri"]})
    assert missing.status_code == 404, missing.text
    assert _get(client, "graph/health", vault=vault)["orphans"]["count"] == 1 + extra


@pytest.mark.asyncio
async def test_mcp_provenance_and_orphans_use_the_same_catalog(graph_installation):
    _client, vault, docs, origin, token, verify = graph_installation
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + token}, verify=verify, timeout=30, follow_redirects=True,
    ) as http_client:
        async with Client(streamable_http_client(origin.rstrip("/") + "/mcp", http_client=http_client)) as client:
            provenance = await client.call_tool("akb_provenance", {"uri": docs[0]["uri"]})
            assert provenance.is_error is False
            result = json.loads(next(item.text for item in provenance.content if item.type == "text"))
            assert "error" not in result, result
            assert result["uri"] == docs[0]["uri"]
            assert result["current_commit"] == docs[0]["current_commit"]
            graph = await client.call_tool("akb_graph", {"vault": vault})
            assert graph.is_error is False
            result = json.loads(next(item.text for item in graph.content if item.type == "text"))
            assert "error" not in result, result
            assert {node["uri"] for node in result["nodes"]} == {
                doc["uri"] for doc in docs if doc["status"] != "archived"
            }


def test_reader_and_outsider_boundaries_are_preserved(graph_installation):
    client, vault, docs, origin, _token, verify = graph_installation
    outsider = os.getenv("AKB_E2E_OUTSIDER_PAT")
    reader, reader_id = os.getenv("AKB_E2E_READER_PAT"), os.getenv("AKB_E2E_READER_ID")
    if not outsider or not reader or not reader_id:
        pytest.skip("provide isolated reader and outsider fixtures for actual ACL checks")
    granted = client.post(f"/api/v1/vaults/{vault}/grant", json={"user_id": reader_id, "role": "reader"})
    assert granted.status_code == 200, granted.text
    paths = [("provenance", {"uri": docs[0]["uri"]}), ("graph/overview", {"vault": vault}),
             ("graph/health", {"vault": vault})]
    for token, status in ((reader, 200), (outsider, 403)):
        with httpx.Client(base_url=origin, verify=verify, timeout=30,
                          headers={"Authorization": "Bearer " + token}) as actor:
            for path, params in paths:
                response = actor.get("/api/v1/" + path, params=params)
                assert response.status_code == status, response.text
