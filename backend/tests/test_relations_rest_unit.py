"""CI-runnable unit coverage for the REST relation write surface
(POST / DELETE /relations) — the err-bridge, the FastAPI request
validation, and the authz gate, none of which the shell e2e
(`test_relations_rest_e2e.sh`) can exercise in CI (it needs a live
deployed backend).

DB-free by construction: every rejection path in
`kg_service.link_resources` / `unlink_resources` returns its err()
envelope *before* `get_pool()` is awaited (bad/non-linkable URI,
self-link), and `_shared_link_vault` rejects cross-vault/malformed URIs
in the route itself. So we drive the REAL services for the reject matrix
and only stub `check_vault_access` (authz) + `get_current_user` (auth)
and, for the happy path, the service call that would touch Postgres.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.routes import knowledge


VAULT = "relunit"
DOC_A = f"akb://{VAULT}/doc/specs/a.md"
DOC_B = f"akb://{VAULT}/doc/specs/b.md"
COLL = f"akb://{VAULT}/coll/specs"
OTHER_VAULT_DOC = "akb://other/doc/specs/c.md"


class _FakeUser:
    user_id = "u-test"
    username = "tester"


@pytest.fixture
def client(monkeypatch):
    """A TestClient over just the knowledge router, with auth/authz stubbed
    to a writer. No lifespan → no DB pool is opened."""
    async def _writer(*_a, **_k):
        return {"vault_id": uuid.uuid4(), "role": "writer", "status": "active"}

    monkeypatch.setattr(knowledge, "check_vault_access", _writer)

    app = FastAPI()
    app.include_router(knowledge.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    return TestClient(app)


# ── POST /relations — request validation (FastAPI/Pydantic) ──────────

def test_post_bad_relation_enum_422(client):
    r = client.post("/api/v1/relations",
                    json={"source": DOC_A, "target": DOC_B, "relation": "bogus"})
    assert r.status_code == 422


def test_post_missing_target_422(client):
    r = client.post("/api/v1/relations",
                    json={"source": DOC_A, "relation": "references"})
    assert r.status_code == 422


# ── POST /relations — service reject matrix (real service, no DB) ────

def test_post_self_link_400(client):
    r = client.post("/api/v1/relations",
                    json={"source": DOC_A, "target": DOC_A, "relation": "references"})
    assert r.status_code == 400


def test_post_cross_vault_400(client):
    r = client.post("/api/v1/relations",
                    json={"source": DOC_A, "target": OTHER_VAULT_DOC, "relation": "references"})
    assert r.status_code == 400


def test_post_malformed_uri_400(client):
    r = client.post("/api/v1/relations",
                    json={"source": "not-a-uri", "target": DOC_B, "relation": "references"})
    assert r.status_code == 400


def test_post_coll_uri_not_linkable_400(client):
    # coll URIs are parseable + carry an identifier, so they clear
    # _shared_link_vault; link_resources must reject them as non-linkable.
    r = client.post("/api/v1/relations",
                    json={"source": COLL, "target": DOC_B, "relation": "references"})
    assert r.status_code == 400


# ── DELETE /relations ────────────────────────────────────────────────

def test_delete_bad_relation_enum_422(client):
    # relation is typed RelationType | None → FastAPI rejects a non-vocab value.
    r = client.request("DELETE", "/api/v1/relations",
                       params={"source": DOC_A, "target": DOC_B, "relation": "bogus"})
    assert r.status_code == 422


def test_delete_coll_uri_400_no_false_success(client):
    # Regression (PR #168): unlink of a coll URI must 400, not a
    # silent 200 {"unlinked": 0}. unlink_resources rejects before get_pool().
    r = client.request("DELETE", "/api/v1/relations",
                       params={"source": COLL, "target": DOC_B, "relation": "references"})
    assert r.status_code == 400


# ── authz gate ───────────────────────────────────────────────────────

def test_reader_forbidden_403(client, monkeypatch):
    async def _deny(*_a, **_k):
        raise HTTPException(status_code=403, detail="writer role required")
    monkeypatch.setattr(knowledge, "check_vault_access", _deny)
    r = client.post("/api/v1/relations",
                    json={"source": DOC_A, "target": DOC_B, "relation": "references"})
    assert r.status_code == 403


# ── happy path: bridge passes a success envelope straight through ────

def test_post_success_passthrough_200(client, monkeypatch):
    async def _ok(*_a, **_k):
        return {"linked": True, "source": DOC_A, "target": DOC_B, "relation": "references"}
    monkeypatch.setattr(knowledge, "link_resources", _ok)
    r = client.post("/api/v1/relations",
                    json={"source": DOC_A, "target": DOC_B, "relation": "references"})
    assert r.status_code == 200
    assert r.json()["linked"] is True


# ── _bridge_service_error unit behaviour (dict guard + unmapped code) ─

def test_bridge_unmapped_code_falls_back_to_400_and_logs(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="akb.api.knowledge"):
        with pytest.raises(HTTPException) as ei:
            knowledge._bridge_service_error({"code": "brand_new_code", "error": "x"})
    assert ei.value.status_code == 400
    assert any("brand_new_code" in rec.message or "brand_new_code" in str(rec.args)
               for rec in caplog.records)


def test_bridge_non_dict_passthrough():
    # A non-dict service result must not crash on .get — passed through.
    sentinel = ["not", "a", "dict"]
    assert knowledge._bridge_service_error(sentinel) is sentinel


def test_bridge_success_dict_passthrough():
    ok = {"linked": True}
    assert knowledge._bridge_service_error(ok) is ok
