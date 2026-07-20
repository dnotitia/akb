"""DB-free runtime serialization contracts for graph and provenance routes."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.routes import knowledge


VAULT = "graphunit"
URI = f"akb://{VAULT}/doc/specs/a.md"


class _FakeUser:
    user_id = "u-test"
    username = "tester"


@pytest.fixture
def client(monkeypatch):
    async def _reader(*_args, **_kwargs):
        return {"vault_id": uuid.uuid4(), "role": "reader", "status": "active"}

    monkeypatch.setattr(knowledge, "check_vault_access", _reader)
    app = FastAPI()
    app.include_router(knowledge.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    return TestClient(app)


def _overview_payload():
    return {
        "nodes": [{"uri": URI, "name": "A", "resource_type": "doc", "degree": 1}],
        "edges": [{
            "source": URI,
            "target": f"akb://{VAULT}/doc/specs/b.md",
            "relation": "references",
            "kind": "explicit",
        }],
        "nodes_total": 2,
        "edges_total": 1,
        "returned": 2,
        "truncated": False,
        "orphans_returned": 0,
        "orphans_truncated": False,
    }


def test_graph_uri_and_legacy_vault_select_distinct_union_branches(client, monkeypatch):
    async def _graph(_vault, resource_uri=None, **_kwargs):
        if resource_uri:
            return {
                "nodes": [{"uri": URI, "name": "A", "resource_type": "doc", "depth": 0}],
                "edges": [],
            }
        return _overview_payload()

    monkeypatch.setattr(knowledge, "get_graph", _graph)
    neighbors = client.get("/api/v1/graph", params={"uri": URI}).json()
    overview = client.get("/api/v1/graph", params={"vault": VAULT}).json()

    assert neighbors["kind"] == "graph_neighbors"
    assert set(neighbors) == {"kind", "nodes", "edges"}
    assert "degree" not in neighbors["nodes"][0]
    assert overview["kind"] == "graph_overview"
    assert set(_overview_payload()).issubset(overview)


def test_overview_and_health_keep_existing_nested_shape(client, monkeypatch):
    async def _overview(*_args, **_kwargs):
        return _overview_payload()

    async def _health(*_args, **_kwargs):
        return {
            "hubs": [{"uri": URI, "name": "A", "resource_type": "doc", "degree": 2}],
            "orphans": {
                "count": 1,
                "sample": [{"uri": f"akb://{VAULT}/doc/orphan.md", "name": "O", "resource_type": "doc"}],
            },
        }

    monkeypatch.setattr(knowledge, "get_overview", _overview)
    monkeypatch.setattr(knowledge, "get_health", _health)
    overview = client.get("/api/v1/graph/overview", params={"vault": VAULT}).json()
    health = client.get("/api/v1/graph/health", params={"vault": VAULT}).json()

    assert overview["kind"] == "graph_overview"
    assert overview["edges"][0]["kind"] == "explicit"
    assert health["kind"] == "graph_health"
    assert health["orphans"]["count"] == 1
    assert "degree" not in health["orphans"]["sample"][0]


def test_provenance_keeps_flat_payload_and_explicit_null(client, monkeypatch):
    class _Conn:
        async def fetchrow(self, *_args):
            return {"vault_id": uuid.uuid4(), "doc_pk": uuid.uuid4()}

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def _pool():
        return _Pool()

    async def _provenance(*_args, **_kwargs):
        return {
            "doc_id": "doc-id",
            "title": "A",
            "path": "specs/a.md",
            "vault": VAULT,
            "uri": URI,
            "created_by": None,
            "created_at": None,
            "updated_at": "2026-07-20T00:00:00+00:00",
            "current_commit": None,
            "relations": [],
        }

    monkeypatch.setattr(knowledge, "get_pool", _pool)
    monkeypatch.setattr(knowledge, "get_provenance", _provenance)
    body = client.get("/api/v1/provenance", params={"uri": URI}).json()
    assert body["kind"] == "provenance"
    assert "provenance" not in body
    assert body["created_by"] is None
    assert body["current_commit"] is None
