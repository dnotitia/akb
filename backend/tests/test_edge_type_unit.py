"""``edge_type`` (strong | weak) on relations — DB-free coverage.

Connection strength is its own attribute: not the relation name (``related_to``,
``derived_from`` … say what a link means) and not the provenance in
``edges.kind`` (implicit = derived from a document, explicit = ``akb_link``).

* an explicit edge is ``strong`` unless its creator said ``weak``;
* an implicit edge is always ``weak`` — computed when served, never stored;
* an explicit edge keeps the value in ``edges.metadata->>'edge_type'``, and a row
  without the key (every ``akb_link`` edge that predates the attribute) reads as
  ``strong``.

This file pins the pieces that need no database: the resolution rule, the write
parameters ``link_resources`` hands to PostgreSQL, the shape of every edge-listing
projection, and the MCP surface. What the SQL itself does with them — the
re-link merge, the legacy rows, the implicit rewrite — runs against a real
PostgreSQL in ``test_edge_type_postgres.py``.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from typing import Any

import pytest

from app.config import settings

# Importing the MCP server builds the revision backend, which creates its Git
# storage directory; point it somewhere writable before that import.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-edge-type-unit-")

from app.services import kg_service
from app.services.kg_service import DEFAULT_EDGE_TYPE, EDGE_TYPES, resolve_edge_type
from mcp_server import server


VAULT = "edgetype"
VAULT_ID = uuid.uuid4()
# Canonical (0.3.0) URIs: `link_resources` stores and returns this form.
A = f"akb://{VAULT}/coll/specs/doc/a.md"
B = f"akb://{VAULT}/coll/specs/doc/b.md"
C = f"akb://{VAULT}/coll/specs/doc/c.md"
D = f"akb://{VAULT}/coll/specs/doc/d.md"
E = f"akb://{VAULT}/coll/specs/doc/e.md"


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


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _use_connection(monkeypatch, conn) -> None:
    async def _pool():
        return _Pool(conn)

    monkeypatch.setattr(kg_service, "get_pool", _pool)


def _forbid_pool(monkeypatch) -> None:
    async def _unexpected_pool():
        raise AssertionError("an invalid request must be refused before PostgreSQL is opened")

    monkeypatch.setattr(kg_service, "get_pool", _unexpected_pool)


# ── The vocabulary and the resolution rule ───────────────────────────────


def test_vocabulary_is_strong_and_weak_and_an_explicit_edge_defaults_to_strong():
    assert EDGE_TYPES == ("strong", "weak")
    assert DEFAULT_EDGE_TYPE == "strong"


@pytest.mark.parametrize(
    ("kind", "stored", "expected"),
    [
        # Implicit edges are derived, so they are weak whatever the row says.
        ("implicit", None, "weak"),
        ("implicit", "weak", "weak"),
        ("implicit", "strong", "weak"),  # a stray key never promotes a derived edge
        (None, None, "weak"),
        # Explicit edges read their stored value ...
        ("explicit", "strong", "strong"),
        ("explicit", "weak", "weak"),
        # ... and a row with no key is every akb_link edge made before this
        # attribute existed: strong, with no backfill.
        ("explicit", None, "strong"),
        # Nothing outside the vocabulary is ever served.
        ("explicit", "medium", "strong"),
        ("explicit", "WEAK", "strong"),
        ("explicit", "", "strong"),
    ],
)
def test_resolve_edge_type(kind, stored, expected):
    assert resolve_edge_type(kind, stored) == expected


# ── link_resources: validation ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["medium", "", "Strong", "WEAK", None, 1, True, ["weak"], {"weak": 1}])
async def test_link_rejects_an_edge_type_outside_the_vocabulary(monkeypatch, bad):
    _forbid_pool(monkeypatch)

    result = await kg_service.link_resources(VAULT, A, B, "related_to", edge_type=bad)

    assert result["code"] == "invalid_argument"
    assert "strong" in result["error"] and "weak" in result["error"]
    assert "linked" not in result


# ── link_resources: what is written ──────────────────────────────────────


class _LinkConnection:
    """Answers just enough of ``link_resources``' SQL to reach the upsert."""

    def __init__(self) -> None:
        self.upserts: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query: str, *_args):
        assert "FROM vaults" in query, query
        return {"id": VAULT_ID}

    async def fetchval(self, _query: str, *_args):
        return 1  # every endpoint exists

    async def execute(self, query: str, *args):
        if "INSERT INTO edges" in query:
            self.upserts.append((query, args))
        return "INSERT 0 1"


async def _link(monkeypatch, **kwargs) -> tuple[dict, dict[str, Any]]:
    """Link A -> B and return (result, the metadata object written for the edge)."""
    conn = _LinkConnection()
    _use_connection(monkeypatch, conn)

    result = await kg_service.link_resources(VAULT, A, B, "related_to", **kwargs)

    ((_query, args),) = conn.upserts
    return result, json.loads(args[7])  # $8 is the metadata parameter


@pytest.mark.asyncio
async def test_link_is_strong_by_default_and_says_so(monkeypatch):
    result, stored = await _link(monkeypatch)

    assert stored == {"edge_type": "strong"}
    assert result == {
        "linked": True, "source": A, "target": B,
        "relation": "related_to", "edge_type": "strong",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("edge_type", EDGE_TYPES)
async def test_link_stores_the_requested_edge_type(monkeypatch, edge_type):
    result, stored = await _link(monkeypatch, edge_type=edge_type)

    assert stored == {"edge_type": edge_type}
    assert result["edge_type"] == edge_type


@pytest.mark.asyncio
async def test_link_keeps_the_callers_metadata_next_to_the_edge_type(monkeypatch):
    caller = {"note": "x", "confidence": 0.5}

    _result, stored = await _link(monkeypatch, metadata=caller, edge_type="weak")

    assert stored == {"note": "x", "confidence": 0.5, "edge_type": "weak"}
    assert caller == {"note": "x", "confidence": 0.5}, "the caller's dict must not be mutated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "edge_type", "expected"),
    [
        ({"edge_type": "weak"}, "strong", "strong"),
        ({"edge_type": "strong"}, "weak", "weak"),
        ({"edge_type": "medium"}, "strong", "strong"),
    ],
)
async def test_the_argument_owns_the_reserved_metadata_key(monkeypatch, metadata, edge_type, expected):
    _result, stored = await _link(monkeypatch, metadata=metadata, edge_type=edge_type)

    assert stored == {"edge_type": expected}


@pytest.mark.asyncio
async def test_link_marks_the_edge_explicit(monkeypatch):
    conn = _LinkConnection()
    _use_connection(monkeypatch, conn)

    await kg_service.link_resources(VAULT, A, B, "related_to", edge_type="weak")

    ((query, _args),) = conn.upserts
    assert "'explicit'" in query


# ── Every edge listing serves edge_type ──────────────────────────────────


def _outgoing(relation: str, target: str, kind: str, stored: str | None) -> dict:
    return {
        "relation_type": relation, "target_uri": target, "target_type": "doc",
        "kind": kind, "stored_edge_type": stored,
    }


def _incoming(relation: str, source: str, kind: str, stored: str | None) -> dict:
    return {
        "relation_type": relation, "source_uri": source, "source_type": "doc",
        "kind": kind, "stored_edge_type": stored,
    }


@pytest.mark.asyncio
async def test_relations_serve_the_resolved_edge_type_in_both_directions(monkeypatch):
    queries: list[str] = []

    class _Connection:
        async def fetch(self, query: str, *_args):
            if "FROM edges e" in query:
                queries.append(query)
                if "e.source_uri = $1" in query:
                    return [
                        _outgoing("references", B, "explicit", "weak"),
                        _outgoing("depends_on", C, "explicit", None),
                        _outgoing("links_to", D, "implicit", None),
                    ]
                return [
                    _incoming("related_to", E, "explicit", "strong"),
                    _incoming("related_to", D, "implicit", "strong"),
                ]
            return []

    _use_connection(monkeypatch, _Connection())

    rows = await kg_service.get_resource_relations(VAULT, A, vault_id=VAULT_ID)

    assert [(r["direction"], r["relation"], r["kind"], r["edge_type"]) for r in rows] == [
        ("outgoing", "references", "explicit", "weak"),
        ("outgoing", "depends_on", "explicit", "strong"),
        ("outgoing", "links_to", "implicit", "weak"),
        ("incoming", "related_to", "explicit", "strong"),
        ("incoming", "related_to", "implicit", "weak"),
    ]
    assert len(queries) == 2
    assert all("metadata->>'edge_type'" in query for query in queries)


@pytest.mark.asyncio
async def test_relations_filtered_by_type_still_serve_edge_type(monkeypatch):
    queries: list[str] = []

    class _Connection:
        async def fetch(self, query: str, *_args):
            if "FROM edges e" in query:
                queries.append(query)
                return [_outgoing("references", B, "explicit", "weak")] if "e.source_uri = $1" in query else []
            return []

    _use_connection(monkeypatch, _Connection())

    rows = await kg_service.get_resource_relations(
        VAULT, A, vault_id=VAULT_ID, relation_type="references",
    )

    assert [(r["relation"], r["edge_type"]) for r in rows] == [("references", "weak")]
    assert all("e.relation_type = $4" in query for query in queries)
    assert all("metadata->>'edge_type'" in query for query in queries)


@pytest.mark.asyncio
async def test_provenance_relations_carry_edge_type(monkeypatch):
    class _Connection:
        async def fetchrow(self, query: str, *_args):
            assert "FROM documents d" in query, query
            return {
                "id": uuid.uuid4(), "title": "A", "path": "specs/a.md", "created_by": None,
                "created_at": None, "updated_at": None, "current_commit": None,
                "metadata": {}, "vault_name": VAULT,
            }

        async def fetch(self, query: str, *_args):
            if "FROM edges e" in query:
                return [_outgoing("references", B, "explicit", "weak")] if "e.source_uri = $1" in query else []
            return []

    _use_connection(monkeypatch, _Connection())

    provenance = await kg_service.get_provenance("doc-id", vault_id=VAULT_ID)

    assert [(r["kind"], r["edge_type"]) for r in provenance["relations"]] == [("explicit", "weak")]


@pytest.mark.asyncio
async def test_graph_neighbors_serve_edge_type_for_every_emitted_edge(monkeypatch):
    queries: list[str] = []

    class _Connection:
        async def fetch(self, query: str, args, *_rest):
            if "FROM edges" in query:
                queries.append(query)
                if "source_uri = ANY" in query:
                    return [
                        {
                            "source_uri": A, "target_uri": B, "target_type": "doc",
                            "relation_type": "references", "kind": "explicit",
                            "stored_edge_type": "weak",
                        },
                        {
                            "source_uri": A, "target_uri": C, "target_type": "doc",
                            "relation_type": "links_to", "kind": "implicit",
                            "stored_edge_type": None,
                        },
                    ] if A in args else []
                return [
                    {
                        "source_uri": D, "target_uri": A, "source_type": "doc",
                        "relation_type": "depends_on", "kind": "explicit",
                        "stored_edge_type": None,
                    },
                ] if A in args else []
            return []

    _use_connection(monkeypatch, _Connection())

    graph = await kg_service.get_graph(VAULT, resource_uri=A, hops=1, limit=20, vault_id=VAULT_ID)

    assert {(e["source"], e["target"], e["relation"]): (e["kind"], e["edge_type"]) for e in graph["edges"]} == {
        (A, B, "references"): ("explicit", "weak"),
        (A, C, "links_to"): ("implicit", "weak"),
        (D, A, "depends_on"): ("explicit", "strong"),
    }
    assert queries and all("metadata->>'edge_type'" in query for query in queries)


@pytest.mark.asyncio
async def test_overview_serves_edge_type_for_every_induced_edge(monkeypatch):
    queries: list[str] = []

    class _Connection:
        async def fetchrow(self, _query: str, *_args):
            return {"edges_total": 3, "nodes_total": 3}

        async def fetch(self, query: str, *_args):
            if "count(*) AS degree" in query:
                return [
                    {"uri": A, "rtype": "doc", "degree": 2},
                    {"uri": B, "rtype": "doc", "degree": 1},
                    {"uri": C, "rtype": "doc", "degree": 1},
                ]
            if "relation_type, kind" in query:
                queries.append(query)
                return [
                    {"source_uri": A, "target_uri": B, "relation_type": "references",
                     "kind": "explicit", "stored_edge_type": "weak"},
                    {"source_uri": A, "target_uri": C, "relation_type": "depends_on",
                     "kind": "explicit", "stored_edge_type": None},
                    {"source_uri": B, "target_uri": C, "relation_type": "links_to",
                     "kind": "implicit", "stored_edge_type": "strong"},
                ]
            return []

    _use_connection(monkeypatch, _Connection())

    overview = await kg_service.get_overview(VAULT, vault_id=VAULT_ID, include_orphans=False)

    assert [(e["relation"], e["kind"], e["edge_type"]) for e in overview["edges"]] == [
        ("references", "explicit", "weak"),
        ("depends_on", "explicit", "strong"),
        ("links_to", "implicit", "weak"),
    ]
    assert len(queries) == 1 and "metadata->>'edge_type'" in queries[0]


# ── MCP ─────────────────────────────────────────────────────────────────


def _mcp_user():
    return server._MCPUser(user_id=str(uuid.uuid4()), username="tester")


def test_the_mcp_argument_gate_accepts_edge_type_on_akb_link_only():
    assert "edge_type" in server._TOOL_ARG_NAMES["akb_link"]
    assert "edge_type" not in server._TOOL_ARG_NAMES["akb_unlink"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, "strong"),
        ({"edge_type": None}, "strong"),  # null means "not specified"
        ({"edge_type": "strong"}, "strong"),
        ({"edge_type": "weak"}, "weak"),
    ],
)
async def test_akb_link_forwards_edge_type_and_defaults_to_strong(monkeypatch, extra, expected):
    seen: dict[str, Any] = {}

    async def _writer(*_args, **_kwargs):
        return {"vault_id": VAULT_ID, "role": "writer"}

    async def _link_resources(vault, source, target, relation, **kwargs):
        seen.update(kwargs)
        return {"linked": True, "source": source, "target": target,
                "relation": relation, "edge_type": kwargs["edge_type"]}

    monkeypatch.setattr(server, "check_vault_access", _writer)
    monkeypatch.setattr(server, "link_resources", _link_resources)

    result = await server._dispatch(
        "akb_link", {"source": A, "target": B, "relation": "related_to", **extra}, _mcp_user(),
    )

    assert seen["edge_type"] == expected
    assert result["edge_type"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["medium", "", "Weak", 0, ["weak"]])
async def test_akb_link_refuses_an_invalid_edge_type_rather_than_defaulting(monkeypatch, bad):
    async def _writer(*_args, **_kwargs):
        return {"vault_id": VAULT_ID, "role": "writer"}

    monkeypatch.setattr(server, "check_vault_access", _writer)
    _forbid_pool(monkeypatch)

    result = await server._dispatch(
        "akb_link", {"source": A, "target": B, "relation": "related_to", "edge_type": bad}, _mcp_user(),
    )

    assert result["code"] == "invalid_argument"
    assert "linked" not in result


@pytest.mark.asyncio
async def test_a_misspelled_edge_type_argument_is_pointed_at_the_real_one(monkeypatch):
    async def _unexpected(*_args, **_kwargs):
        raise AssertionError("an unknown argument must be refused before the handler runs")

    monkeypatch.setattr(server, "check_vault_access", _unexpected)

    result = await server._dispatch(
        "akb_link", {"source": A, "target": B, "relation": "related_to", "edge-type": "weak"}, _mcp_user(),
    )

    assert result["code"] == "unknown_argument"
    assert "edge_type" in result["hint"]
