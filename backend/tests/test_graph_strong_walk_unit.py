"""Strength- and direction-aware neighbourhood walk (``edge_type`` and ``direction`` on
``/graph?uri=`` and ``akb_graph``) — DB-free.

``edge_type`` ("strong" | "weak"; omitted = every edge) restricts the BFS itself,
in both directions, so the ``limit`` node cap is spent on the neighbours the
caller asked for instead of being spent on edges that are then filtered away.
The vault overview is not strength-aware and a request that pairs it with
``edge_type`` is refused.

``direction`` ("both", the default | "outward") chooses which edges of a reached
node the walk follows. ``outward`` takes its outgoing edges plus what was derived
from it and what depends on it (the incoming ``derived_from`` and ``depends_on``
ones), so the walk stops at shared context instead of fanning out through a hub.
It composes with ``edge_type`` and, like it, needs ``uri``.

This file pins what needs no database: the SQL fragments (the twin of
``resolve_edge_type``, and the incoming-edge narrowing), that the walk applies
them to the right fetches, how ``hops`` / ``limit`` / ``truncated`` behave, and
the REST and MCP parameter handling. That the fragments *select the right rows*
is a fact about PostgreSQL and runs there, in ``test_graph_strong_walk_postgres.py``.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.routes import knowledge
from app.config import settings

# Importing the MCP server builds the revision backend, which creates its Git
# storage directory; point it somewhere writable before that import.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-strong-walk-unit-")

from app.services import kg_service
from app.services.kg_service import (
    DEFAULT_GRAPH_DIRECTION,
    EDGE_TYPES,
    GRAPH_DIRECTIONS,
    LINK_RELATION_TYPES,
    OUTWARD_INCOMING_RELATIONS,
    edge_strength_sql,
    incoming_walk_sql,
    resolve_edge_type,
)
from mcp_server import server
from mcp_server.tools import TOOLS

VAULT = "strongwalk"
VAULT_ID = uuid.uuid4()


def uri(name: str) -> str:
    return f"akb://{VAULT}/coll/n/doc/{name}.md"


STRONG_SQL = edge_strength_sql("strong")
WEAK_SQL = edge_strength_sql("weak")
OUTWARD_SQL = incoming_walk_sql("outward")


# ── The SQL twin of resolve_edge_type ────────────────────────────────────


def test_the_strength_predicate_is_the_documented_rule():
    assert edge_strength_sql(None) == ""
    assert edge_strength_sql("strong") == (
        "(kind = 'explicit' AND metadata->>'edge_type' IS DISTINCT FROM 'weak')"
    )
    assert edge_strength_sql("weak") == "(kind <> 'explicit' OR metadata->>'edge_type' = 'weak')"
    assert edge_strength_sql("strong", alias="e") == (
        "(e.kind = 'explicit' AND e.metadata->>'edge_type' IS DISTINCT FROM 'weak')"
    )
    assert edge_strength_sql("weak", alias="e") == "(e.kind <> 'explicit' OR e.metadata->>'edge_type' = 'weak')"


@pytest.mark.parametrize("bad", ["medium", "", "Strong", "strong; DROP TABLE edges", 1, ["weak"]])
def test_a_value_outside_the_vocabulary_never_reaches_sql(bad):
    with pytest.raises(ValueError, match="edge_type"):
        edge_strength_sql(bad)  # type: ignore[arg-type]


def test_the_predicates_partition_the_edges_the_way_resolve_edge_type_does():
    """The documented rule, written out, is a partition that agrees with `resolve_edge_type`.

    `strong` / `weak` below restate what `edge_strength_sql` says in words (the
    exact-text test above pins the SQL to it). That they split every
    (kind, stored) shape into two buckets, and agree with `resolve_edge_type`
    on each, is checked without a database; that PostgreSQL evaluates the
    fragments the same way is checked in `test_graph_strong_walk_postgres.py`.
    """
    def strong(kind, stored):
        return kind == "explicit" and stored != "weak"

    def weak(kind, stored):
        return kind != "explicit" or stored == "weak"

    for kind in ("implicit", "explicit"):
        for stored in (None, "strong", "weak", "medium", "Weak", ""):
            served = resolve_edge_type(kind, stored)
            assert strong(kind, stored) == (served == "strong"), (kind, stored)
            assert weak(kind, stored) == (served == "weak"), (kind, stored)
            assert strong(kind, stored) != weak(kind, stored), (kind, stored)  # a partition


# ── The direction fragment and its vocabulary ────────────────────────────


def test_the_direction_vocabulary_is_both_and_outward_and_defaults_to_both():
    assert GRAPH_DIRECTIONS == ("both", "outward")
    assert DEFAULT_GRAPH_DIRECTION == "both"
    # What was derived from a node and what depends on it: the relations `outward` follows
    # against the arrow, every one of them a relation `akb_link` can write. An ordered
    # tuple, so the SQL built from it reads the same in every process.
    assert OUTWARD_INCOMING_RELATIONS == ("derived_from", "depends_on")
    assert set(OUTWARD_INCOMING_RELATIONS) <= set(LINK_RELATION_TYPES)


def test_the_direction_predicate_is_the_documented_rule():
    assert incoming_walk_sql(None) == ""
    assert incoming_walk_sql("both") == ""
    assert incoming_walk_sql("outward") == "relation_type IN ('derived_from', 'depends_on')"


def test_the_direction_predicate_is_built_from_the_constant_alone(monkeypatch):
    monkeypatch.setattr(kg_service, "OUTWARD_INCOMING_RELATIONS", ("references", "implements", "related_to"))

    assert incoming_walk_sql("outward") == "relation_type IN ('references', 'implements', 'related_to')"


@pytest.mark.parametrize("bad", ["sideways", "", "Outward", "incoming", "outward; DROP TABLE edges", 1, ["outward"]])
def test_a_direction_outside_the_vocabulary_never_reaches_sql(bad):
    with pytest.raises(ValueError, match="direction"):
        incoming_walk_sql(bad)  # type: ignore[arg-type]


# ── The walk, against an in-memory edge table ────────────────────────────


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


class _Edges:
    """An `edges` table the BFS can fetch from.

    The strength predicate is applied the way PostgreSQL would apply it — chosen
    by which exact fragment the query carries — so a walk that forgot it on one
    direction, or built it for the wrong strength, is caught here. The fragment's
    own semantics are not re-implemented: the row is kept or dropped by
    `resolve_edge_type`, the rule the fragment is the twin of. The direction
    fragment is applied the same way: a query that carries it keeps only rows of
    the relations it names, so it is caught on a fetch where it does not belong.
    """

    def __init__(self, *edges: tuple) -> None:
        # (source, target, kind, stored edge_type[, relation]); `references` unless said
        self.edges = [edge if len(edge) == 5 else (*edge, "references") for edge in edges]
        self.queries: list[str] = []

    def _keep(self, query: str, kind: str, stored: str | None, relation: str) -> bool:
        served = resolve_edge_type(kind, stored)
        if STRONG_SQL in query and served != "strong":
            return False
        if WEAK_SQL in query and served != "weak":
            return False
        if OUTWARD_SQL in query and relation not in OUTWARD_INCOMING_RELATIONS:
            return False
        return True

    async def fetch(self, query: str, *args):
        if "FROM edges" not in query:
            return []  # the label lookups: nothing resolves, nodes keep their placeholder names
        self.queries.append(query)
        seeds = set(args[0])
        outgoing = "source_uri = ANY" in query
        return [
            {
                "source_uri": s, "target_uri": t, "source_type": "doc", "target_type": "doc",
                "relation_type": relation, "kind": kind, "stored_edge_type": stored,
            }
            for s, t, kind, stored, relation in self.edges
            if (s if outgoing else t) in seeds and self._keep(query, kind, stored, relation)
        ]


async def _walk(
    edges: _Edges, start: str, *, hops: int = 2, limit: int = 50, edge_type=None, direction=None,
):
    nodes: dict[str, dict] = {}
    edge_list: list[dict] = []
    truncated = await kg_service._bfs_collect(
        edges, VAULT_ID, VAULT, start, hops, limit, nodes, edge_list,
        edge_type=edge_type, direction=direction,
    )
    return nodes, edge_list, truncated


def _names(nodes: dict[str, dict]) -> set[str]:
    return {u.rsplit("/", 1)[-1].removesuffix(".md") for u in nodes}


def _mixed() -> _Edges:
    """c with one strong and one weak neighbour each way, and a chain behind each."""
    c = uri("c")
    return _Edges(
        (c, uri("s-out"), "explicit", None),  # strong (key absent: predates the attribute)
        (uri("s-in"), c, "explicit", "strong"),  # strong, incoming
        (c, uri("w-out"), "explicit", "weak"),  # weak, explicit
        (uri("w-in"), c, "implicit", None),  # weak, implicit, incoming
        (uri("s-out"), uri("s-out-2"), "explicit", "strong"),  # a strong second hop
        (uri("s-out"), uri("x-weak-2"), "explicit", "weak"),  # a weak second hop off a strong node
        (uri("w-out"), uri("w-out-2"), "explicit", "strong"),  # strong, but behind a weak edge
    )


@pytest.mark.asyncio
async def test_without_edge_type_the_walk_follows_every_edge_and_adds_no_predicate():
    edges = _mixed()

    nodes, edge_list, truncated = await _walk(edges, uri("c"))

    assert _names(nodes) == {"c", "s-out", "s-in", "w-out", "w-in", "s-out-2", "x-weak-2", "w-out-2"}
    assert len(edge_list) == 7
    assert truncated is False
    assert len(edges.queries) == 6  # outgoing + incoming, once per wave (hops=2 is three waves)
    assert not any("IS DISTINCT FROM" in q or "kind <> 'explicit'" in q for q in edges.queries)


@pytest.mark.asyncio
async def test_a_strong_walk_goes_through_strong_edges_only_in_both_directions():
    edges = _mixed()

    nodes, edge_list, truncated = await _walk(edges, uri("c"), edge_type="strong")

    assert _names(nodes) == {"c", "s-out", "s-in", "s-out-2"}
    assert {e["edge_type"] for e in edge_list} == {"strong"}
    assert len(edge_list) == 3
    # Never visited — not merely filtered out afterwards.
    assert {"w-out", "w-in", "x-weak-2", "w-out-2"}.isdisjoint(_names(nodes))
    assert truncated is False


@pytest.mark.asyncio
async def test_a_weak_walk_is_the_mirror_image():
    nodes, edge_list, _ = await _walk(_mixed(), uri("c"), edge_type="weak")

    assert _names(nodes) == {"c", "w-out", "w-in"}
    assert {e["edge_type"] for e in edge_list} == {"weak"}
    assert len(edge_list) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("edge_type", EDGE_TYPES)
async def test_the_predicate_is_on_the_outgoing_and_the_incoming_fetch(edge_type):
    edges = _mixed()
    fragment = edge_strength_sql(edge_type)

    await _walk(edges, uri("c"), edge_type=edge_type)

    outgoing = [q for q in edges.queries if "source_uri = ANY" in q]
    incoming = [q for q in edges.queries if "target_uri = ANY" in q]
    assert outgoing and incoming
    assert all(fragment in q for q in outgoing + incoming)
    # ... and still inside the vault scope, ANDed on rather than replacing it.
    assert all("starts_with(source_uri" in q and "starts_with(target_uri" in q for q in outgoing + incoming)


@pytest.mark.asyncio
async def test_a_strong_walk_respects_hops():
    nodes, _, truncated = await _walk(_mixed(), uri("c"), hops=1, edge_type="strong")

    assert _names(nodes) == {"c", "s-out", "s-in"}  # s-out-2 is two hops out
    assert truncated is False  # the radius is not a cut
    assert {n["depth"] for n in nodes.values()} == {0, 1}


@pytest.mark.asyncio
async def test_the_node_cap_is_spent_on_strong_neighbours_only():
    """10 strong and 30 weak neighbours: a strong walk fits in 11, an unfiltered one cannot."""
    c = uri("hub")
    edges = _Edges(
        *[(c, uri(f"strong-{i}"), "explicit", None) for i in range(10)],
        *[(c, uri(f"weak-{i}"), "explicit", "weak") for i in range(30)],
    )

    strong_nodes, strong_edges, strong_truncated = await _walk(edges, c, hops=1, limit=11, edge_type="strong")
    all_nodes, _, all_truncated = await _walk(edges, c, hops=1, limit=11)

    assert len(strong_nodes) == 11 and strong_truncated is False
    assert all(name.startswith(("strong-", "hub")) for name in _names(strong_nodes))
    assert len(strong_edges) == 10
    assert len(all_nodes) == 11 and all_truncated is True  # 40 neighbours, 10 slots


# ── Truncation is reported honestly ──────────────────────────────────────


def _star(count: int) -> _Edges:
    return _Edges(*[(uri("c"), uri(f"leaf-{i}"), "explicit", None) for i in range(count)])


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [None, "outward"])  # the leaves are outgoing, so both walks take them
@pytest.mark.parametrize(
    ("leaves", "limit", "kept", "truncated"),
    [
        (3, 10, 4, False),  # room to spare
        (3, 4, 4, False),  # an exact fit is not a cut
        (3, 3, 3, True),  # one leaf did not fit
        (3, 1, 1, True),  # only the centre fits
        (0, 1, 1, False),  # an isolated centre has nothing to cut
        (0, 5, 1, False),
    ],
)
async def test_truncated_means_a_qualifying_node_did_not_fit(leaves, limit, kept, truncated, direction):
    nodes, edge_list, reported = await _walk(_star(leaves), uri("c"), hops=1, limit=limit, direction=direction)

    assert len(nodes) == kept
    assert reported is truncated
    # An edge to a node that was cut is dropped, never served as a stub.
    assert all(e["source"] in nodes and e["target"] in nodes for e in edge_list)


@pytest.mark.asyncio
async def test_a_node_reached_from_two_parents_is_not_counted_twice():
    c = uri("c")
    diamond = _Edges(
        (c, uri("b"), "explicit", None), (c, uri("d"), "explicit", None),
        (uri("b"), uri("e"), "explicit", None), (uri("d"), uri("e"), "explicit", None),
    )

    nodes, _, truncated = await _walk(diamond, c, hops=2, limit=4)

    assert _names(nodes) == {"c", "b", "d", "e"}
    assert truncated is False  # e was queued twice and still fits exactly


@pytest.mark.asyncio
async def test_the_cap_reached_between_waves_is_reported():
    c = uri("c")
    chain = _Edges((c, uri("b"), "explicit", None), (uri("b"), uri("d"), "explicit", None))

    nodes, _, truncated = await _walk(chain, c, hops=2, limit=2)

    assert _names(nodes) == {"c", "b"}
    assert truncated is True  # d, a second-hop neighbour, was waiting when the cap closed


@pytest.mark.asyncio
async def test_nodes_beyond_the_radius_are_not_a_truncation():
    c = uri("c")
    chain = _Edges((c, uri("b"), "explicit", None), (uri("b"), uri("d"), "explicit", None))

    nodes, _, truncated = await _walk(chain, c, hops=1, limit=50)

    assert _names(nodes) == {"c", "b"}
    assert truncated is False


# ── Direction: an outward walk stops at shared context ───────────────────


def _incidents() -> _Edges:
    """An incident, the cluster it ran on, a sibling incident on that same cluster,
    and a postmortem derived from the incident that also names a service.

    The incidents name the cluster as context (`related_to`), not as something they
    depend on: an incoming `depends_on` is followed, so a cluster that incidents
    *depended on* would be walked through to every one of them.
    """
    return _Edges(
        (uri("incident"), uri("cluster"), "explicit", None, "related_to"),
        (uri("other-incident"), uri("cluster"), "explicit", None, "related_to"),
        (uri("pm"), uri("incident"), "explicit", None, "derived_from"),
        (uri("pm"), uri("service"), "explicit", None, "references"),
    )


def _short(u: str) -> str:
    return u.rsplit("/", 1)[-1].removesuffix(".md")


def _edge_names(edge_list: list[dict]) -> set[tuple[str, str, str]]:
    return {(_short(e["source"]), _short(e["target"]), e["relation"]) for e in edge_list}


@pytest.mark.asyncio
async def test_an_outward_walk_follows_its_own_context_and_what_was_derived_from_it():
    nodes, edge_list, truncated = await _walk(_incidents(), uri("incident"), direction="outward")

    assert _names(nodes) == {"incident", "cluster", "pm", "service"}
    assert "other-incident" not in _names(nodes)  # it only points at the cluster, which is shared context
    assert _edge_names(edge_list) == {
        ("incident", "cluster", "related_to"),  # outgoing
        ("pm", "incident", "derived_from"),  # incoming, but derived_from
        ("pm", "service", "references"),  # the postmortem's own outgoing edge, one hop further out
    }
    assert {_short(u): n["depth"] for u, n in nodes.items()} == {
        "incident": 0, "cluster": 1, "pm": 1, "service": 2,
    }
    assert truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [None, "both"])
async def test_both_is_the_walk_it_always_was(direction):
    edges = _incidents()

    nodes, edge_list, truncated = await _walk(edges, uri("incident"), direction=direction)

    assert _names(nodes) == {"incident", "cluster", "pm", "other-incident", "service"}
    assert len(edge_list) == 4 and truncated is False
    assert not any(OUTWARD_SQL in q for q in edges.queries)  # no narrowing on either fetch


@pytest.mark.asyncio
async def test_an_outward_walk_respects_hops():
    nodes, edge_list, truncated = await _walk(_incidents(), uri("incident"), hops=1, direction="outward")

    assert _names(nodes) == {"incident", "cluster", "pm"}  # the postmortem's edge to the service is a second hop
    assert _edge_names(edge_list) == {
        ("incident", "cluster", "related_to"), ("pm", "incident", "derived_from"),
    }
    assert truncated is False  # the radius is not a cut


@pytest.mark.asyncio
async def test_started_on_a_hub_an_outward_walk_returns_the_hub_alone():
    nodes, edge_list, truncated = await _walk(_incidents(), uri("cluster"), direction="outward")
    everything, _, _ = await _walk(_incidents(), uri("cluster"))

    assert _names(nodes) == {"cluster"} and edge_list == [] and truncated is False
    assert _names(everything) == {"cluster", "incident", "other-incident", "pm"}


@pytest.mark.asyncio
async def test_derived_from_is_followed_against_the_arrow_from_every_reached_node():
    lineage = _Edges(
        (uri("pm"), uri("incident"), "explicit", None, "derived_from"),
        (uri("summary"), uri("pm"), "explicit", None, "derived_from"),  # derived from the postmortem, not the incident
        (uri("incident"), uri("raw-log"), "explicit", None, "derived_from"),  # the incident derives from a log: outgoing
    )

    nodes, edge_list, _ = await _walk(lineage, uri("incident"), direction="outward")

    assert _names(nodes) == {"incident", "pm", "summary", "raw-log"}
    assert len(edge_list) == 3


# What an outward walk takes against the arrow, written out. It is stated here, not read from
# `OUTWARD_INCOMING_RELATIONS`, so a change to the rule has to change this test too.
FOLLOWED_AGAINST_THE_ARROW = {"derived_from", "depends_on"}  # what was derived from it; what depends on it
NOT_FOLLOWED_AGAINST_THE_ARROW = {"related_to", "references", "links_to", "implements", "attached_to"}


def test_every_relation_is_decided_as_followed_against_the_arrow_or_not():
    """A relation added to `akb_link` (or stored by extraction) must be decided here."""
    assert FOLLOWED_AGAINST_THE_ARROW.isdisjoint(NOT_FOLLOWED_AGAINST_THE_ARROW)
    assert FOLLOWED_AGAINST_THE_ARROW | NOT_FOLLOWED_AGAINST_THE_ARROW == {*LINK_RELATION_TYPES, "links_to"}
    assert set(OUTWARD_INCOMING_RELATIONS) == FOLLOWED_AGAINST_THE_ARROW


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", sorted(FOLLOWED_AGAINST_THE_ARROW | NOT_FOLLOWED_AGAINST_THE_ARROW))
async def test_only_derived_from_and_depends_on_are_followed_against_the_arrow(relation):
    pointing_at = _Edges((uri("other"), uri("c"), "explicit", None, relation))
    pointed_to = _Edges((uri("c"), uri("other"), "explicit", None, relation))

    inbound_nodes, inbound_edges, _ = await _walk(pointing_at, uri("c"), direction="outward")
    outbound_nodes, outbound_edges, _ = await _walk(pointed_to, uri("c"), direction="outward")

    assert _names(outbound_nodes) == {"c", "other"} and len(outbound_edges) == 1  # outgoing: always
    if relation in FOLLOWED_AGAINST_THE_ARROW:
        assert _names(inbound_nodes) == {"c", "other"} and len(inbound_edges) == 1
    else:
        assert _names(inbound_nodes) == {"c"} and inbound_edges == []


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", sorted(NOT_FOLLOWED_AGAINST_THE_ARROW))
async def test_both_still_takes_every_incoming_relation(relation):
    """`both` is the walk it always was: the relations `outward` leaves out are taken."""
    pointing_at = _Edges((uri("other"), uri("c"), "explicit", None, relation))

    for direction in (None, "both"):
        nodes, edge_list, _ = await _walk(pointing_at, uri("c"), direction=direction)

        assert _names(nodes) == {"c", "other"} and len(edge_list) == 1
        assert not any(OUTWARD_SQL in q for q in pointing_at.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("edge_type", [None, *EDGE_TYPES])
async def test_the_direction_predicate_is_on_the_incoming_fetch_only(edge_type):
    edges = _incidents()

    await _walk(edges, uri("incident"), edge_type=edge_type, direction="outward")

    outgoing = [q for q in edges.queries if "source_uri = ANY" in q]
    incoming = [q for q in edges.queries if "target_uri = ANY" in q]
    assert outgoing and incoming
    assert all(OUTWARD_SQL in q for q in incoming)
    assert not any(OUTWARD_SQL in q for q in outgoing)  # an outgoing edge is followed whatever its relation
    strength = edge_strength_sql(edge_type)
    if strength:  # ... and the strength still narrows both, ANDed on beside it
        assert all(strength in q for q in outgoing + incoming)
    assert all("starts_with(source_uri" in q and "starts_with(target_uri" in q for q in outgoing + incoming)


def _strength_mix() -> _Edges:
    return _Edges(
        (uri("incident"), uri("cluster"), "explicit", None, "related_to"),  # strong, outgoing
        (uri("incident"), uri("owner"), "explicit", "weak", "references"),  # weak, outgoing
        (uri("pm"), uri("incident"), "explicit", None, "derived_from"),  # strong, derived from it
        (uri("draft"), uri("incident"), "explicit", "weak", "derived_from"),  # weak, derived from it
        (uri("log"), uri("incident"), "implicit", None, "derived_from"),  # implicit, so weak
        (uri("sibling"), uri("cluster"), "explicit", None, "related_to"),  # strong, but shared context
    )


@pytest.mark.asyncio
async def test_outward_composes_with_edge_type():
    start = uri("incident")

    outward = await _walk(_strength_mix(), start, direction="outward")
    strong = await _walk(_strength_mix(), start, edge_type="strong", direction="outward")
    weak = await _walk(_strength_mix(), start, edge_type="weak", direction="outward")
    strong_both = await _walk(_strength_mix(), start, edge_type="strong")

    assert _names(outward[0]) == {"incident", "cluster", "owner", "pm", "draft", "log"}
    assert _names(strong[0]) == {"incident", "cluster", "pm"}
    assert {e["edge_type"] for e in strong[1]} == {"strong"}
    assert _names(weak[0]) == {"incident", "owner", "draft", "log"}
    assert {e["edge_type"] for e in weak[1]} == {"weak"}
    # Each narrows what the other leaves: the strong walk that is not outward reaches the sibling.
    assert _names(strong_both[0]) == {"incident", "cluster", "pm", "sibling"}


@pytest.mark.asyncio
async def test_an_edge_the_walk_did_not_follow_is_not_emitted_even_between_visited_nodes():
    """x points at y and both are reached. x sits on the radius, so its own edges are
    never read, and y only takes an incoming edge when it is `derived_from`."""
    web = _Edges(
        (uri("s"), uri("y"), "explicit", None), (uri("s"), uri("p"), "explicit", None),
        (uri("p"), uri("x"), "explicit", None), (uri("x"), uri("y"), "explicit", None),
    )

    both_nodes, both_edges, _ = await _walk(web, uri("s"))
    outward_nodes, outward_edges, _ = await _walk(web, uri("s"), direction="outward")

    assert _names(both_nodes) == _names(outward_nodes) == {"s", "y", "p", "x"}
    assert _edge_names(both_edges) == {
        ("s", "y", "references"), ("s", "p", "references"), ("p", "x", "references"), ("x", "y", "references"),
    }
    assert _edge_names(outward_edges) == _edge_names(both_edges) - {("x", "y", "references")}
    # No stub endpoints either way: everything emitted joins two returned nodes.
    assert all(e["source"] in outward_nodes and e["target"] in outward_nodes for e in outward_edges)


@pytest.mark.asyncio
async def test_a_hub_does_not_spend_the_node_cap_on_an_outward_walk():
    """The shape seen on real data: every incident points at one cluster."""
    hub = uri("cluster")
    edges = _Edges(*[(uri(f"incident-{i}"), hub, "explicit", None, "related_to") for i in range(300)])
    start = uri("incident-0")

    fan_out, _, fan_out_truncated = await _walk(edges, start, limit=200, edge_type="strong")
    nodes, edge_list, truncated = await _walk(edges, start, limit=200, edge_type="strong", direction="outward")

    assert len(fan_out) == 200 and fan_out_truncated is True  # incident, cluster, then every other incident
    assert _names(nodes) == {"incident-0", "cluster"} and len(edge_list) == 1
    assert truncated is False


@pytest.mark.asyncio
async def test_neighbours_an_outward_walk_does_not_follow_are_not_a_cut():
    """Five documents point at c. With room for only c, nothing that qualified was left out."""
    edges = _Edges(*[(uri(f"leaf-{i}"), uri("c"), "explicit", None) for i in range(5)])

    nodes, edge_list, truncated = await _walk(edges, uri("c"), hops=1, limit=1, direction="outward")
    default_nodes, _, default_truncated = await _walk(edges, uri("c"), hops=1, limit=1)

    assert _names(nodes) == {"c"} and edge_list == [] and truncated is False
    assert len(default_nodes) == 1 and default_truncated is True  # the same cap does cut the default walk


# ── Direction: an outward walk reaches what depends on a node ────────────


def _services() -> _Edges:
    """An incident that names a service, what depends on that service and on those, and the
    documents that merely point at it.

    web and batch depend on api, mobile depends on web (not on api itself), and api depends on db.
    """
    return _Edges(
        (uri("incident"), uri("api"), "explicit", None, "related_to"),  # the service it names
        (uri("web"), uri("api"), "explicit", None, "depends_on"),  # web depends on api: affected by it
        (uri("mobile"), uri("web"), "explicit", None, "depends_on"),  # mobile depends on web: one level further
        (uri("batch"), uri("api"), "explicit", "weak", "depends_on"),  # a weak dependent
        (uri("api"), uri("db"), "explicit", None, "depends_on"),  # api depends on db: outgoing, always followed
        (uri("runbook"), uri("api"), "explicit", None, "references"),  # points at api, depends on nothing
        (uri("notes"), uri("api"), "implicit", None, "links_to"),  # a body link: weak, and not a dependency
        (uri("sibling"), uri("api"), "explicit", None, "related_to"),  # another incident on the same service
    )


@pytest.mark.asyncio
async def test_an_outward_walk_reaches_what_depends_on_a_service_a_level_per_hop():
    one = await _walk(_services(), uri("incident"), hops=1, direction="outward")
    two = await _walk(_services(), uri("incident"), hops=2, direction="outward")
    three = await _walk(_services(), uri("incident"), hops=3, direction="outward")

    assert _names(one[0]) == {"incident", "api"}
    # Two hops out: api's dependents (web, batch) against the arrow, and what api depends on (db) with it.
    assert _names(two[0]) == {"incident", "api", "db", "web", "batch"}
    assert _edge_names(two[1]) == {
        ("incident", "api", "related_to"), ("api", "db", "depends_on"),
        ("web", "api", "depends_on"), ("batch", "api", "depends_on"),
    }
    # Three hops out: the dependents of a dependent.
    assert _names(three[0]) == {"incident", "api", "db", "web", "batch", "mobile"}
    assert _edge_names(three[1]) == _edge_names(two[1]) | {("mobile", "web", "depends_on")}
    assert {_short(u): n["depth"] for u, n in three[0].items()} == {
        "incident": 0, "api": 1, "db": 2, "web": 2, "batch": 2, "mobile": 3,
    }
    # ... and none of what merely points at the service: not a dependency, so shared context.
    assert {"runbook", "notes", "sibling"}.isdisjoint(_names(three[0]))
    assert all(truncated is False for _, _, truncated in (one, two, three))


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [None, "both"])
async def test_both_takes_the_dependents_and_everything_else_that_points_at_the_service(direction):
    nodes, edge_list, truncated = await _walk(_services(), uri("incident"), hops=3, direction=direction)

    assert _names(nodes) == {"incident", "api", "db", "web", "batch", "mobile", "runbook", "notes", "sibling"}
    assert len(edge_list) == 8 and truncated is False


@pytest.mark.asyncio
async def test_depends_on_is_followed_against_the_arrow_from_every_reached_node():
    chain = _Edges(
        (uri("web"), uri("api"), "explicit", None, "depends_on"),
        (uri("mobile"), uri("web"), "explicit", None, "depends_on"),  # depends on web, not on api
        (uri("api"), uri("db"), "explicit", None, "depends_on"),  # api depends on db: outgoing
        (uri("report"), uri("db"), "explicit", None, "depends_on"),  # depends on db, which api reaches outward
    )

    from_api = await _walk(chain, uri("api"), hops=3, direction="outward")
    from_mobile = await _walk(chain, uri("mobile"), hops=4, direction="outward")

    assert _names(from_api[0]) == {"api", "web", "mobile", "db", "report"} and len(from_api[1]) == 4
    # mobile -> web -> api -> db are outgoing edges, followed whatever their relation; report depends
    # on db, a node the walk reached, so it is a dependent of a reached node and is taken against the arrow.
    assert _names(from_mobile[0]) == {"mobile", "web", "api", "db", "report"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edge_type", "start", "expected"),
    [
        (None, "incident", {"incident", "api", "db", "web", "batch", "mobile"}),
        ("strong", "incident", {"incident", "api", "db", "web", "mobile"}),  # batch is a weak dependent
        ("weak", "incident", {"incident"}),  # the way out of the incident is strong
        ("weak", "api", {"api", "batch"}),  # the body link into api is weak but not a dependency
        ("strong", "api", {"api", "db", "web", "mobile"}),
    ],
)
async def test_outward_dependents_compose_with_edge_type(edge_type, start, expected):
    nodes, edge_list, _ = await _walk(_services(), uri(start), hops=3, edge_type=edge_type, direction="outward")

    assert _names(nodes) == expected
    if edge_type:
        assert {e["edge_type"] for e in edge_list} <= {edge_type}


@pytest.mark.asyncio
async def test_an_implicit_depends_on_from_frontmatter_is_a_dependent_too():
    """A service document that lists `depends_on:` in its frontmatter leaves an implicit (weak) edge."""
    services = _Edges(
        (uri("web"), uri("api"), "implicit", None, "depends_on"),
        (uri("mobile"), uri("web"), "implicit", None, "depends_on"),
    )

    every = await _walk(services, uri("api"), hops=2, direction="outward")
    weak = await _walk(services, uri("api"), hops=2, edge_type="weak", direction="outward")
    strong = await _walk(services, uri("api"), hops=2, edge_type="strong", direction="outward")

    assert _names(every[0]) == _names(weak[0]) == {"api", "web", "mobile"}
    assert _names(strong[0]) == {"api"}  # an implicit edge is weak


@pytest.mark.asyncio
async def test_a_service_everything_depends_on_is_walked_through_and_the_cap_still_applies():
    """The other side of the hub test: a dependency hub is not stopped at, and `limit` bounds it."""
    core = uri("core")
    edges = _Edges(*[(uri(f"service-{i}"), core, "explicit", None, "depends_on") for i in range(300)])

    nodes, edge_list, truncated = await _walk(edges, core, hops=1, limit=200, direction="outward")
    related = _Edges(*[(uri(f"service-{i}"), core, "explicit", None, "related_to") for i in range(300)])
    related_nodes, related_edges, related_truncated = await _walk(related, core, hops=1, limit=200, direction="outward")

    assert len(nodes) == 200 and truncated is True  # core and 199 of its 300 dependents
    assert all(e["target"] == core for e in edge_list)
    assert _names(related_nodes) == {"core"} and related_edges == [] and related_truncated is False


# ── get_graph: parameter handling ────────────────────────────────────────


def _forbid_pool(monkeypatch) -> None:
    async def _pool():
        raise AssertionError("a refused request must not open PostgreSQL")

    monkeypatch.setattr(kg_service, "get_pool", _pool)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["medium", "", "Strong", 0, ["weak"]])
async def test_an_invalid_edge_type_is_refused_before_the_database(monkeypatch, bad):
    _forbid_pool(monkeypatch)

    result = await kg_service.get_graph(VAULT, uri("c"), vault_id=VAULT_ID, edge_type=bad)

    assert result["code"] == "invalid_argument"
    assert "edge_type must be one of ['strong', 'weak']" in result["error"]
    assert "nodes" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("edge_type", EDGE_TYPES)
async def test_edge_type_without_a_uri_is_refused_not_ignored(monkeypatch, edge_type):
    """The overview is not strength-aware; answering with every edge would mislead."""
    _forbid_pool(monkeypatch)

    result = await kg_service.get_graph(VAULT, None, vault_id=VAULT_ID, edge_type=edge_type)

    assert result["code"] == "invalid_argument"
    assert "`uri`" in result["error"]
    assert "hint" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["sideways", "", "Outward", "incoming", 0, ["outward"]])
async def test_an_invalid_direction_is_refused_before_the_database(monkeypatch, bad):
    _forbid_pool(monkeypatch)

    result = await kg_service.get_graph(VAULT, uri("c"), vault_id=VAULT_ID, direction=bad)

    assert result["code"] == "invalid_argument"
    assert "direction must be one of ['both', 'outward']" in result["error"]
    assert "nodes" not in result


@pytest.mark.asyncio
async def test_a_bad_direction_is_a_bad_value_before_it_is_a_missing_uri(monkeypatch):
    _forbid_pool(monkeypatch)

    result = await kg_service.get_graph(VAULT, None, vault_id=VAULT_ID, direction="sideways")

    assert result["code"] == "invalid_argument"
    assert "direction must be one of" in result["error"]


@pytest.mark.asyncio
async def test_outward_without_a_uri_is_refused_not_ignored(monkeypatch):
    """The overview has no direction to restrict; answering with every edge would mislead."""
    _forbid_pool(monkeypatch)

    result = await kg_service.get_graph(VAULT, None, vault_id=VAULT_ID, direction="outward")

    assert result["code"] == "invalid_argument"
    assert "`uri`" in result["error"] and "outward" in result["error"]
    assert "hint" in result and "nodes" not in result


@pytest.mark.asyncio
async def test_both_without_a_uri_is_the_overview_it_always_was(monkeypatch):
    """`both` is the default and what the overview does, so naming it asks for nothing new."""
    seen: dict = {}

    async def _overview(vault, **kwargs):
        seen.update(kwargs, vault=vault)
        return {"nodes": [], "edges": [], "truncated": False}

    monkeypatch.setattr(kg_service, "get_overview", _overview)

    result = await kg_service.get_graph(VAULT, None, limit=77, vault_id=VAULT_ID, direction="both")

    assert result == {"nodes": [], "edges": [], "truncated": False}
    assert seen == {"vault": VAULT, "vault_id": VAULT_ID, "top_k": 77}


@pytest.mark.asyncio
async def test_without_edge_type_the_overview_is_untouched(monkeypatch):
    seen: dict = {}

    async def _overview(vault, **kwargs):
        seen.update(kwargs, vault=vault)
        return {"nodes": [], "edges": [], "truncated": False}

    monkeypatch.setattr(kg_service, "get_overview", _overview)

    result = await kg_service.get_graph(VAULT, None, limit=77, vault_id=VAULT_ID)

    assert result == {"nodes": [], "edges": [], "truncated": False}
    assert seen == {"vault": VAULT, "vault_id": VAULT_ID, "top_k": 77}


@pytest.mark.asyncio
async def test_a_uri_outside_the_vault_returns_an_empty_untruncated_graph(monkeypatch):
    _forbid_pool(monkeypatch)

    result = await kg_service.get_graph(
        VAULT, "akb://elsewhere/doc/x.md", vault_id=VAULT_ID, edge_type="strong",
    )

    assert result == {"nodes": [], "edges": [], "truncated": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [None, "both", "outward"])
@pytest.mark.parametrize("edge_type", [None, "strong", "weak"])
async def test_the_neighbourhood_forwards_edge_type_and_direction_and_reports_truncation(
    monkeypatch, edge_type, direction,
):
    seen: dict = {}

    async def _bfs(conn, vault_id, vault, start, hops, limit, nodes, edges, *, edge_type=None, direction=None):
        seen.update(hops=hops, limit=limit, edge_type=edge_type, direction=direction)
        nodes[start] = {"uri": start, "resource_type": "doc", "name": "x", "depth": 0}
        return True

    monkeypatch.setattr(kg_service, "_bfs_collect", _bfs)

    async def _pool():
        return _Pool(object())

    monkeypatch.setattr(kg_service, "get_pool", _pool)

    result = await kg_service.get_graph(
        VAULT, uri("c"), hops=3, limit=9, vault_id=VAULT_ID, edge_type=edge_type, direction=direction,
    )

    assert seen == {"hops": 3, "limit": 9, "edge_type": edge_type, "direction": direction}
    assert result["truncated"] is True
    assert [n["uri"] for n in result["nodes"]] == [uri("c")]


# ── REST ─────────────────────────────────────────────────────────────────


class _FakeUser:
    user_id = "u-test"
    username = "tester"


@pytest.fixture
def client(monkeypatch):
    async def _reader(*_args, **_kwargs):
        return {"vault_id": VAULT_ID, "role": "reader", "status": "active"}

    monkeypatch.setattr(knowledge, "check_vault_access", _reader)
    app = FastAPI()
    app.include_router(knowledge.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    return TestClient(app)


def test_rest_forwards_edge_type_to_the_service_and_serves_truncated(client, monkeypatch):
    seen: dict = {}

    async def _graph(vault, resource_uri=None, **kwargs):
        seen.update(kwargs, vault=vault, uri=resource_uri)
        return {"nodes": [], "edges": [], "truncated": True}

    monkeypatch.setattr(knowledge, "get_graph", _graph)

    body = client.get("/api/v1/graph", params={"uri": uri("c"), "edge_type": "strong"}).json()
    default = client.get("/api/v1/graph", params={"uri": uri("c")}).json()

    assert seen["edge_type"] is None  # the second call: omitted means every edge
    assert body["kind"] == default["kind"] == "graph_neighbors"
    assert body["truncated"] is True


def test_rest_passes_the_strength_through(client, monkeypatch):
    seen: list = []

    async def _graph(vault, resource_uri=None, **kwargs):
        seen.append(kwargs["edge_type"])
        return {"nodes": [], "edges": [], "truncated": False}

    monkeypatch.setattr(knowledge, "get_graph", _graph)
    for value in ("strong", "weak"):
        assert client.get("/api/v1/graph", params={"uri": uri("c"), "edge_type": value}).status_code == 200

    assert seen == ["strong", "weak"]


@pytest.mark.parametrize("bad", ["medium", "", "Strong", "strong,weak"])
def test_rest_rejects_an_invalid_edge_type(client, monkeypatch, bad):
    async def _graph(*_args, **_kwargs):
        raise AssertionError("an invalid edge_type must be refused before the service runs")

    monkeypatch.setattr(knowledge, "get_graph", _graph)

    response = client.get("/api/v1/graph", params={"uri": uri("c"), "edge_type": bad})

    assert response.status_code == 422


def test_rest_refuses_edge_type_on_the_vault_overview(client):
    response = client.get("/api/v1/graph", params={"vault": VAULT, "edge_type": "strong"})

    assert response.status_code == 400
    assert "`uri`" in response.json()["detail"]


def test_rest_documents_edge_type_as_an_enum_on_the_neighbourhood_route():
    from app.main import app

    operation = app.openapi()["paths"]["/api/v1/graph"]["get"]
    param = next(p for p in operation["parameters"] if p["name"] == "edge_type")

    assert param["required"] is False
    options = param["schema"].get("anyOf") or [param["schema"]]
    assert next(o["enum"] for o in options if "enum" in o) == list(EDGE_TYPES)
    assert "neighbourhood" in param["description"].lower()


def test_rest_forwards_direction_to_the_service(client, monkeypatch):
    seen: list = []

    async def _graph(vault, resource_uri=None, **kwargs):
        seen.append((kwargs["edge_type"], kwargs["direction"]))
        return {"nodes": [], "edges": [], "truncated": False}

    monkeypatch.setattr(knowledge, "get_graph", _graph)
    for extra in ({}, {"direction": "outward"}, {"direction": "both"}, {"edge_type": "strong", "direction": "outward"}):
        assert client.get("/api/v1/graph", params={"uri": uri("c"), **extra}).status_code == 200

    assert seen == [(None, None), (None, "outward"), (None, "both"), ("strong", "outward")]


@pytest.mark.parametrize("bad", ["sideways", "", "Outward", "incoming", "outward,both"])
def test_rest_rejects_an_invalid_direction(client, monkeypatch, bad):
    async def _graph(*_args, **_kwargs):
        raise AssertionError("an invalid direction must be refused before the service runs")

    monkeypatch.setattr(knowledge, "get_graph", _graph)

    response = client.get("/api/v1/graph", params={"uri": uri("c"), "direction": bad})

    assert response.status_code == 422


def test_rest_refuses_outward_on_the_vault_overview(client):
    response = client.get("/api/v1/graph", params={"vault": VAULT, "direction": "outward"})

    assert response.status_code == 400
    assert "`uri`" in response.json()["detail"]


def test_rest_accepts_both_on_the_vault_overview(client, monkeypatch):
    async def _overview(vault, **kwargs):
        return {
            "nodes": [], "edges": [], "nodes_total": 0, "edges_total": 0, "returned": 0,
            "truncated": False, "orphans_returned": 0, "orphans_truncated": False,
        }

    monkeypatch.setattr(kg_service, "get_overview", _overview)

    response = client.get("/api/v1/graph", params={"vault": VAULT, "direction": "both"})

    assert response.status_code == 200
    assert response.json()["kind"] == "graph_overview"


def test_rest_documents_direction_as_an_enum_on_the_neighbourhood_route():
    from app.main import app

    paths = app.openapi()["paths"]
    param = next(p for p in paths["/api/v1/graph"]["get"]["parameters"] if p["name"] == "direction")

    assert param["required"] is False
    options = param["schema"].get("anyOf") or [param["schema"]]
    assert next(o["enum"] for o in options if "enum" in o) == list(GRAPH_DIRECTIONS)
    assert "neighbourhood" in param["description"].lower()
    assert "derived_from" in param["description"] and "depends_on" in param["description"]
    # The overview has no direction to choose.
    assert "direction" not in {p["name"] for p in paths["/api/v1/graph/overview"]["get"]["parameters"]}


# ── MCP ──────────────────────────────────────────────────────────────────


def _mcp_user():
    return server._MCPUser(user_id=str(uuid.uuid4()), username="tester")


def test_akb_graph_advertises_edge_type_from_the_shared_vocabulary():
    graph = next(tool for tool in TOOLS if tool.name == "akb_graph")
    edge_type = graph.input_schema["properties"]["edge_type"]

    assert edge_type["enum"] == list(EDGE_TYPES)
    assert "edge_type" not in graph.input_schema.get("required", [])
    assert "uri" in edge_type["description"]
    assert "edge_type" in server._TOOL_ARG_NAMES["akb_graph"]
    assert "edge_type" not in server._TOOL_ARG_NAMES["akb_relations"]


@pytest.fixture
def reader(monkeypatch):
    async def _access(*_args, **_kwargs):
        return {"vault_id": VAULT_ID, "role": "reader"}

    monkeypatch.setattr(server, "check_vault_access", _access)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "expected"),
    [({}, None), ({"edge_type": None}, None), ({"edge_type": "strong"}, "strong"), ({"edge_type": "weak"}, "weak")],
)
async def test_akb_graph_forwards_edge_type(monkeypatch, reader, extra, expected):
    seen: dict = {}

    async def _graph(vault, **kwargs):
        seen.update(kwargs)
        return {"nodes": [], "edges": [], "truncated": False}

    monkeypatch.setattr(server, "get_graph", _graph)

    await server._dispatch("akb_graph", {"uri": uri("c"), "hops": 1, **extra}, _mcp_user())

    assert seen["edge_type"] == expected
    assert (seen["hops"], seen["limit"]) == (1, 50)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["medium", "", "Weak", 0])
async def test_akb_graph_refuses_an_invalid_edge_type(monkeypatch, reader, bad):
    _forbid_pool(monkeypatch)

    result = await server._dispatch("akb_graph", {"uri": uri("c"), "edge_type": bad}, _mcp_user())

    assert result["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_akb_graph_refuses_edge_type_with_a_vault_and_no_uri(monkeypatch, reader):
    _forbid_pool(monkeypatch)

    result = await server._dispatch("akb_graph", {"vault": VAULT, "edge_type": "strong"}, _mcp_user())

    assert result["code"] == "invalid_argument"
    assert "uri" in result["error"] and result["hint"]


def test_akb_graph_advertises_direction_from_the_shared_vocabulary():
    graph = next(tool for tool in TOOLS if tool.name == "akb_graph")
    direction = graph.input_schema["properties"]["direction"]

    assert direction["enum"] == list(GRAPH_DIRECTIONS)
    assert direction["default"] == DEFAULT_GRAPH_DIRECTION == "both"
    assert "direction" not in graph.input_schema.get("required", [])
    assert "uri" in direction["description"]
    assert "derived_from" in direction["description"] and "depends_on" in direction["description"]
    assert "direction=outward" in graph.description  # one short sentence on the tool itself
    assert "derived_from" in graph.description and "depends_on" in graph.description
    assert "direction" in server._TOOL_ARG_NAMES["akb_graph"]
    # `akb_relations` has a `direction` of its own, over another vocabulary: not the same parameter.
    relations = next(tool for tool in TOOLS if tool.name == "akb_relations")
    assert relations.input_schema["properties"]["direction"]["enum"] == ["incoming", "outgoing", "both"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "expected"),
    [({}, None), ({"direction": None}, None), ({"direction": "both"}, "both"), ({"direction": "outward"}, "outward")],
)
async def test_akb_graph_forwards_direction(monkeypatch, reader, extra, expected):
    seen: dict = {}

    async def _graph(vault, **kwargs):
        seen.update(kwargs)
        return {"nodes": [], "edges": [], "truncated": False}

    monkeypatch.setattr(server, "get_graph", _graph)

    await server._dispatch(
        "akb_graph", {"uri": uri("c"), "hops": 1, "edge_type": "strong", **extra}, _mcp_user(),
    )

    assert seen["direction"] == expected
    assert seen["edge_type"] == "strong"  # the two compose; neither replaces the other
    assert (seen["hops"], seen["limit"]) == (1, 50)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["sideways", "", "Outward", "incoming", 0])
async def test_akb_graph_refuses_an_invalid_direction(monkeypatch, reader, bad):
    _forbid_pool(monkeypatch)

    result = await server._dispatch("akb_graph", {"uri": uri("c"), "direction": bad}, _mcp_user())

    assert result["code"] == "invalid_argument"
    assert "direction must be one of" in result["error"]


@pytest.mark.asyncio
async def test_akb_graph_refuses_outward_with_a_vault_and_no_uri(monkeypatch, reader):
    _forbid_pool(monkeypatch)

    result = await server._dispatch("akb_graph", {"vault": VAULT, "direction": "outward"}, _mcp_user())

    assert result["code"] == "invalid_argument"
    assert "uri" in result["error"] and result["hint"]


@pytest.mark.asyncio
async def test_akb_graph_accepts_both_with_a_vault_and_no_uri(monkeypatch, reader):
    async def _overview(vault, **kwargs):
        return {"nodes": [], "edges": [], "truncated": False}

    monkeypatch.setattr(kg_service, "get_overview", _overview)

    result = await server._dispatch("akb_graph", {"vault": VAULT, "direction": "both"}, _mcp_user())

    assert result == {"nodes": [], "edges": [], "truncated": False}
