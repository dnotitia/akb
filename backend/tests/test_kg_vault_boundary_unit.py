"""Vault-boundary regression tests for structured and projected relations."""

from __future__ import annotations

import uuid

import pytest

from app.exceptions import ValidationError
from app.services import kg_service


VAULT = "alpha"
OTHER = "beta"
VAULT_ID = uuid.uuid4()
SOURCE = f"akb://{VAULT}/coll/specs/doc/a.md"
TARGET = f"akb://{VAULT}/coll/specs/doc/b.md"
FOREIGN = f"akb://{OTHER}/coll/private/doc/hidden.md"


class _Acquire:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_new_structured_cross_vault_ref_is_rejected_without_target_lookup():
    with pytest.raises(ValidationError, match="ordinary Markdown link"):
        kg_service.validate_new_structured_relation_refs(VAULT, [FOREIGN])


def test_existing_cross_vault_ref_remains_editable_but_inert():
    kg_service.validate_new_structured_relation_refs(
        VAULT,
        [FOREIGN],
        existing_refs=[FOREIGN],
    )


@pytest.mark.asyncio
async def test_explicit_cross_vault_and_owner_mismatch_fail_before_pool(monkeypatch):
    async def _unexpected_pool():
        raise AssertionError("boundary rejection must not open PostgreSQL")

    monkeypatch.setattr(kg_service, "get_pool", _unexpected_pool)

    cross = await kg_service.link_resources(VAULT, SOURCE, FOREIGN, "related_to")
    owner_mismatch = await kg_service.link_resources(OTHER, SOURCE, TARGET, "related_to")

    assert cross["code"] == "invalid_argument"
    assert owner_mismatch["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_unlink_removes_explicit_edges_only(monkeypatch):
    observed: dict[str, object] = {}

    class _Connection:
        async def fetchval(self, _query: str, *_args):
            return VAULT

        async def execute(self, query: str, *args):
            observed["query"] = query
            observed["args"] = args
            return "DELETE 0"

    async def _pool():
        return _Pool(_Connection())

    monkeypatch.setattr(kg_service, "get_pool", _pool)
    result = await kg_service.unlink_resources(
        SOURCE,
        TARGET,
        relation_type="related_to",
        vault_id=VAULT_ID,
    )

    assert result["unlinked"] == 0
    assert "kind = 'explicit'" in str(observed["query"])


@pytest.mark.asyncio
async def test_implicit_cross_vault_link_skips_before_catalog_or_insert():
    class _NoLookupConnection:
        async def fetchval(self, *_args):
            raise AssertionError("cross-vault extraction must not query a catalog")

        async def execute(self, *_args):
            raise AssertionError("cross-vault extraction must not insert an edge")

    stored = await kg_service._store_edge(
        _NoLookupConnection(),
        VAULT_ID,
        VAULT,
        SOURCE,
        "doc",
        FOREIGN,
        "links_to",
    )

    assert stored is False


@pytest.mark.asyncio
async def test_relation_projection_omits_foreign_uri_and_exposes_edge_kind(monkeypatch):
    edge_queries: list[str] = []

    class _Connection:
        async def fetch(self, query: str, *_args):
            if "FROM edges e" in query:
                edge_queries.append(query)
                if "e.source_uri = $1" in query:
                    return [
                        {
                            "relation_type": "references",
                            "target_uri": TARGET,
                            "target_type": "doc",
                            "kind": "explicit",
                        },
                        {
                            "relation_type": "related_to",
                            "target_uri": FOREIGN,
                            "target_type": "doc",
                            "kind": "implicit",
                        },
                    ]
                return []
            if "FROM documents" in query:
                return [{"path": "specs/b.md", "title": "Visible target"}]
            return []

    conn = _Connection()

    async def _pool():
        return _Pool(conn)

    monkeypatch.setattr(kg_service, "get_pool", _pool)
    rows = await kg_service.get_resource_relations(
        VAULT,
        SOURCE,
        vault_id=VAULT_ID,
    )

    assert rows == [
        {
            "direction": "outgoing",
            "relation": "references",
            "uri": TARGET,
            "resource_type": "doc",
            "kind": "explicit",
            "name": "Visible target",
        }
    ]
    assert edge_queries
    assert all("starts_with(e.source_uri" in query for query in edge_queries)
    assert all("starts_with(e.target_uri" in query for query in edge_queries)


@pytest.mark.asyncio
async def test_bfs_never_materializes_foreign_endpoint():
    edge_queries: list[str] = []

    class _Connection:
        async def fetch(self, query: str, args, *_rest):
            if "FROM edges" in query:
                edge_queries.append(query)
                if "source_uri = ANY" in query and SOURCE in args:
                    return [
                        {
                            "source_uri": SOURCE,
                            "target_uri": TARGET,
                            "target_type": "doc",
                            "relation_type": "references",
                            "kind": "explicit",
                        },
                        {
                            "source_uri": SOURCE,
                            "target_uri": FOREIGN,
                            "target_type": "doc",
                            "relation_type": "related_to",
                            "kind": "implicit",
                        },
                    ]
                return []
            if "FROM documents" in query:
                return [
                    {"path": "specs/a.md", "title": "A"},
                    {"path": "specs/b.md", "title": "B"},
                ]
            return []

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    await kg_service._bfs_collect(
        _Connection(),
        VAULT_ID,
        VAULT,
        SOURCE,
        1,
        20,
        nodes,
        edges,
    )

    assert FOREIGN not in nodes
    assert all(FOREIGN not in (edge["source"], edge["target"]) for edge in edges)
    assert edge_queries
    assert all("starts_with(source_uri" in query for query in edge_queries)
    assert all("starts_with(target_uri" in query for query in edge_queries)


@pytest.mark.asyncio
async def test_overview_and_health_scope_counts_to_both_uri_authorities(monkeypatch):
    observed: list[tuple[str, tuple]] = []

    class _Connection:
        async def fetchrow(self, query: str, *args):
            observed.append((query, args))
            return {"edges_total": 0, "nodes_total": 0}

        async def fetch(self, query: str, *args):
            observed.append((query, args))
            return []

    conn = _Connection()

    async def _pool():
        return _Pool(conn)

    monkeypatch.setattr(kg_service, "get_pool", _pool)

    overview = await kg_service.get_overview(
        VAULT,
        vault_id=VAULT_ID,
        include_orphans=False,
    )
    health = await kg_service.get_health(VAULT, vault_id=VAULT_ID)

    assert overview["edges_total"] == 0
    assert health == {"hubs": [], "orphans": {"count": 0, "sample": []}}
    scoped_queries = [query for query, _args in observed if "FROM edges" in query]
    assert scoped_queries
    assert all("starts_with(source_uri" in query for query in scoped_queries)
    assert all("starts_with(target_uri" in query for query in scoped_queries)
    assert all(
        f"akb://{VAULT}/" in args
        for query, args in observed
        if "FROM edges" in query
    )
