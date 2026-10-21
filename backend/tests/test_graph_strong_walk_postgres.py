"""Real-PostgreSQL regressions for the strength-aware neighbourhood walk.

The DB-free half is ``test_graph_strong_walk_unit.py``. What only a database can
settle is here: that the SQL fragment ``edge_strength_sql`` selects exactly the
rows ``resolve_edge_type`` would serve as that strength — for every shape the
``edges.kind`` / ``edges.metadata`` pair can take, including the legacy ones
(no key, a NULL or non-object ``metadata``, a value outside the vocabulary, a
stray key on an implicit row) — and that the BFS spends its ``limit`` node cap
on the neighbours it was asked for.

``edge_type=strong`` walks and returns only strong edges, in both directions; a
neighbour reachable only through a weak edge is never visited. ``weak`` is the
mirror image; omitted means every edge, as before. The vault overview is not
strength-aware and is left exactly as it was.

``direction=outward`` chooses which edges of a reached node the walk follows: its
outgoing edges plus what was derived from it and what depends on it (the incoming
``derived_from`` and ``depends_on`` ones). Another document that merely points at
a node it reaches (a sibling incident on the same cluster, ``related_to``) is not
followed, so the walk stops at shared context instead of fanning out through a
hub; a service that ``depends_on`` a reached node is, a level per hop. ``both``
(the default) is the walk as it always was. The two compose, and what a walk
returns is the edges it followed.

Set ``AKB_TEST_DSN`` to a PostgreSQL 16 + pgvector instance; ``REQUIRE_REAL_PG=1``
turns a missing instance from a skip into a failure.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.config import settings

# Importing the MCP server builds the revision backend, which creates its Git
# storage directory; point it somewhere writable before that import.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-strong-walk-pg-")

from app.api.deps import get_current_user
from app.api.routes import knowledge
from app.services import document_counters, kg_service
from app.services.kg_service import (
    LINK_RELATION_TYPES,
    OUTWARD_INCOMING_RELATIONS,
    resolve_edge_type,
    store_document_relations,
)
from app.services.uri_service import doc_uri
from mcp_server import server

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def pool():
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")

    pool = await asyncpg.create_pool(dsn=_DSN, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute((_BACKEND / "app" / "db" / "init.sql").read_text())
    try:
        yield pool
    finally:
        await pool.close()


@dataclass
class World:
    pool: asyncpg.Pool
    vault: str
    vault_id: uuid.UUID

    def uri(self, name: str) -> str:
        return doc_uri(self.vault, f"n/{name}.md")

    async def docs(self, *names: str) -> None:
        async with self.pool.acquire() as conn:
            for name in names:
                await conn.execute(
                    "INSERT INTO documents (id, vault_id, path, title) VALUES ($1, $2, $3, $4)",
                    uuid.uuid4(), self.vault_id, f"n/{name}.md", f"Title of {name}",
                )

    async def link(self, source: str, target: str, relation: str = "related_to", **kwargs: Any) -> None:
        result = await kg_service.link_resources(
            self.vault, self.uri(source), self.uri(target), relation, **kwargs,
        )
        assert result.get("linked") is True, result

    async def seed_edge(
        self, source: str, target: str, *, kind: str = "explicit", metadata: str | None = "{}",
        relation: str | None = None,
    ) -> None:
        """Insert a row exactly as an older release (or a bare writer) would have."""
        relation = relation or ("links_to" if kind == "implicit" else "related_to")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO edges (vault_id, source_uri, target_uri, relation_type, source_type, target_type, "
                "metadata, kind) VALUES ($1, $2, $3, $4, 'doc', 'doc', $5::jsonb, $6)",
                self.vault_id, self.uri(source), self.uri(target), relation, metadata, kind,
            )

    async def extract(self, source: str, *, body: str = "") -> None:
        """Run the implicit extraction a document write runs for ``source``."""
        async with self.pool.acquire() as conn:
            await store_document_relations(conn, self.vault_id, self.vault, f"n/{source}.md", [], [], [], body)

    async def extract_frontmatter(
        self, source: str, *, depends_on: tuple[str, ...] = (), related_to: tuple[str, ...] = (),
    ) -> None:
        """Run the implicit extraction a document write runs for the frontmatter lists of ``source``."""
        async with self.pool.acquire() as conn:
            await store_document_relations(
                conn, self.vault_id, self.vault, f"n/{source}.md",
                [self.uri(name) for name in depends_on], [self.uri(name) for name in related_to], [], "",
            )

    async def walk(self, name: str, **kwargs: Any) -> dict:
        return await kg_service.get_graph(self.vault, self.uri(name), vault_id=self.vault_id, **kwargs)

    def names(self, graph: dict) -> set[str]:
        prefix = f"akb://{self.vault}/coll/n/doc/"
        return {n["uri"].removeprefix(prefix).removesuffix(".md") for n in graph["nodes"]}

    def edge_names(self, graph: dict) -> set[tuple[str, str, str]]:
        """Every returned edge as (source, target, relation), by document name."""
        prefix = f"akb://{self.vault}/coll/n/doc/"

        def short(uri: str) -> str:
            return uri.removeprefix(prefix).removesuffix(".md")

        return {(short(e["source"]), short(e["target"]), e["relation"]) for e in graph["edges"]}

    async def seed_many(self, sources: list[str], target: str, relation: str = "related_to") -> None:
        """One explicit strong edge from each of ``sources`` to ``target``, as ``akb_link`` leaves it."""
        async with self.pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO documents (id, vault_id, path, title) VALUES ($1, $2, $3, $4)",
                [(uuid.uuid4(), self.vault_id, f"n/{name}.md", f"Title of {name}") for name in sources],
            )
            await conn.executemany(
                "INSERT INTO edges (vault_id, source_uri, target_uri, relation_type, source_type, target_type, "
                "metadata, kind) VALUES ($1, $2, $3, $4, 'doc', 'doc', '{}'::jsonb, 'explicit')",
                [(self.vault_id, self.uri(name), self.uri(target), relation) for name in sources],
            )


@pytest_asyncio.fixture
async def world(pool, monkeypatch):
    name = f"_test_strong_walk_{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        vault_id = await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id", name, f"/tmp/{name}.git",
        )

    async def _get_pool():
        return pool

    monkeypatch.setattr(kg_service, "get_pool", _get_pool)
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: False)
    try:
        yield World(pool=pool, vault=name, vault_id=vault_id)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)


async def _mixed(world: World) -> None:
    """c: a strong and a weak neighbour each way, and a chain behind each."""
    await world.docs("c", "s-out", "s-in", "w-out", "w-in", "s-out-2", "x-weak-2", "w-out-2")
    await world.link("c", "s-out", "depends_on")  # explicit, strong
    await world.link("s-in", "c", "implements")  # explicit, strong, incoming
    await world.link("c", "w-out", "references", edge_type="weak")  # explicit, weak
    await world.extract("w-in", body=f"See [c]({world.uri('c')}).")  # implicit, weak, incoming
    await world.link("s-out", "s-out-2", "depends_on")  # a strong second hop
    await world.link("s-out", "x-weak-2", "references", edge_type="weak")  # a weak second hop off a strong node
    await world.link("w-out", "w-out-2", "depends_on")  # strong, but behind a weak edge


# ── The walk over real rows ──────────────────────────────────────────────


async def test_without_edge_type_every_edge_is_walked_as_before(world):
    await _mixed(world)

    graph = await world.walk("c")

    assert world.names(graph) == {"c", "s-out", "s-in", "w-out", "w-in", "s-out-2", "x-weak-2", "w-out-2"}
    assert {e["edge_type"] for e in graph["edges"]} == {"strong", "weak"}
    assert graph["truncated"] is False


async def test_a_strong_walk_goes_through_strong_edges_only_in_both_directions(world):
    await _mixed(world)

    graph = await world.walk("c", edge_type="strong")

    assert world.names(graph) == {"c", "s-out", "s-in", "s-out-2"}
    assert {e["edge_type"] for e in graph["edges"]} == {"strong"}
    assert {(e["source"], e["target"]) for e in graph["edges"]} == {
        (world.uri("c"), world.uri("s-out")),  # outgoing
        (world.uri("s-in"), world.uri("c")),  # incoming
        (world.uri("s-out"), world.uri("s-out-2")),  # second hop, through the strong node
    }
    # `w-out-2` is reachable by a strong edge, but only past a weak one: not visited.
    assert "w-out-2" not in world.names(graph)
    assert graph["truncated"] is False


async def test_a_weak_walk_is_the_mirror_image(world):
    await _mixed(world)

    graph = await world.walk("c", edge_type="weak")

    assert world.names(graph) == {"c", "w-out", "w-in"}
    assert {e["edge_type"] for e in graph["edges"]} == {"weak"}
    assert {e["kind"] for e in graph["edges"]} == {"explicit", "implicit"}  # explicit-weak and implicit


async def test_a_strong_walk_respects_hops(world):
    await _mixed(world)

    one = await world.walk("c", hops=1, edge_type="strong")
    two = await world.walk("c", hops=2, edge_type="strong")

    assert world.names(one) == {"c", "s-out", "s-in"}
    assert world.names(two) == {"c", "s-out", "s-in", "s-out-2"}
    assert {n["depth"] for n in one["nodes"]} == {0, 1}
    assert one["truncated"] is False  # the radius is not a cut


async def test_a_node_with_no_edge_of_that_strength_is_returned_alone(world):
    await world.docs("lonely", "other")
    await world.link("lonely", "other", edge_type="weak")

    graph = await world.walk("lonely", edge_type="strong")

    assert world.names(graph) == {"lonely"}
    assert graph["edges"] == [] and graph["truncated"] is False


# ── The cap is spent on the neighbours asked for ─────────────────────────


async def _hub(world: World, strong: int, weak: int) -> None:
    await world.docs("hub", *(f"strong-{i}" for i in range(strong)), *(f"weak-{i}" for i in range(weak)))
    for i in range(strong):
        await world.link("hub", f"strong-{i}")
    for i in range(weak):
        await world.link("hub", f"weak-{i}", edge_type="weak")


async def test_thirty_weak_neighbours_do_not_crowd_ten_strong_ones_out(world):
    await _hub(world, strong=10, weak=30)

    strong = await world.walk("hub", hops=1, limit=11, edge_type="strong")
    everything = await world.walk("hub", hops=1, limit=11)

    assert len(strong["nodes"]) == 11 and strong["truncated"] is False
    assert {n for n in world.names(strong) if n != "hub"} == {f"strong-{i}" for i in range(10)}
    assert len(strong["edges"]) == 10
    # 40 neighbours for 10 slots: which ten is the database's row order, so the
    # unfiltered walk is only held to being cut, never to a particular ten.
    assert len(everything["nodes"]) == 11 and everything["truncated"] is True


@pytest.mark.parametrize(
    ("limit", "kept", "truncated"),
    [(50, 11, False), (11, 11, False), (10, 10, True), (3, 3, True), (1, 1, True)],
)
async def test_truncated_reports_a_cut_on_real_rows(world, limit, kept, truncated):
    await _hub(world, strong=10, weak=0)

    graph = await world.walk("hub", hops=1, limit=limit, edge_type="strong")

    assert len(graph["nodes"]) == kept
    assert graph["truncated"] is truncated
    assert all(e["source"] in {n["uri"] for n in graph["nodes"]} for e in graph["edges"])  # no stub endpoints


async def test_a_cut_in_a_later_wave_is_reported(world):
    await world.docs("c", "b", "d1", "d2")
    await world.link("c", "b")
    await world.link("b", "d1")
    await world.link("b", "d2")

    cut = await world.walk("c", hops=2, limit=3, edge_type="strong")
    fits = await world.walk("c", hops=2, limit=4, edge_type="strong")

    assert len(cut["nodes"]) == 3 and cut["truncated"] is True
    assert len(fits["nodes"]) == 4 and fits["truncated"] is False


async def test_the_default_walk_reports_truncation_too(world):
    await _hub(world, strong=5, weak=5)

    cut = await world.walk("hub", hops=1, limit=4)
    full = await world.walk("hub", hops=1, limit=11)

    assert cut["truncated"] is True and len(cut["nodes"]) == 4
    assert full["truncated"] is False and len(full["nodes"]) == 11


# ── The SQL fragment is the twin of resolve_edge_type ────────────────────

# (kind, edges.metadata as written, what `metadata->>'edge_type'` extracts)
SHAPES: list[tuple[str, str | None, str | None]] = [
    ("explicit", None, None),  # a NULL metadata column
    ("explicit", "{}", None),
    ("explicit", "[]", None),
    ("explicit", '"weak"', None),
    ("explicit", "null", None),
    ("explicit", '{"edge_type": null}', None),
    ("explicit", '{"note": "x"}', None),
    ("explicit", '{"edge_type": "strong"}', "strong"),
    ("explicit", '{"edge_type": "weak"}', "weak"),
    ("explicit", '{"edge_type": "medium"}', "medium"),
    ("explicit", '{"edge_type": "Weak"}', "Weak"),
    ("explicit", '{"edge_type": ""}', ""),
    ("implicit", None, None),
    ("implicit", "{}", None),
    ("implicit", '{"edge_type": "strong"}', "strong"),  # a stray key never promotes a derived edge
    ("implicit", '{"edge_type": "weak"}', "weak"),
]


async def test_the_predicates_select_what_resolve_edge_type_serves_for_every_stored_shape(world):
    names = [f"{kind}-{i}" for i, (kind, _, _) in enumerate(SHAPES)]
    await world.docs("c", *[f"{n}-out" for n in names], *[f"{n}-in" for n in names])
    for name, (kind, metadata, _stored) in zip(names, SHAPES, strict=True):
        await world.seed_edge("c", f"{name}-out", kind=kind, metadata=metadata)  # outgoing
        await world.seed_edge(f"{name}-in", "c", kind=kind, metadata=metadata)  # incoming

    expected = {"strong": {"c"}, "weak": {"c"}}
    for name, (kind, _metadata, stored) in zip(names, SHAPES, strict=True):
        expected[resolve_edge_type(kind, stored)].update({f"{name}-out", f"{name}-in"})
    assert expected["strong"] != {"c"} and expected["weak"] != {"c"}, "the matrix must exercise both strengths"

    unfiltered = await world.walk("c", hops=1, limit=200)
    strong = await world.walk("c", hops=1, limit=200, edge_type="strong")
    weak = await world.walk("c", hops=1, limit=200, edge_type="weak")

    assert world.names(unfiltered) == {"c"} | expected["strong"] | expected["weak"]
    assert world.names(strong) == expected["strong"]
    assert world.names(weak) == expected["weak"]
    # The walk and the label agree row by row: whatever the walk returned, each
    # edge is served with the strength that was asked for.
    assert {e["edge_type"] for e in strong["edges"]} == {"strong"}
    assert {e["edge_type"] for e in weak["edges"]} == {"weak"}
    assert {e["edge_type"] for e in unfiltered["edges"]} == {"strong", "weak"}
    assert len(strong["edges"]) + len(weak["edges"]) == len(unfiltered["edges"]) == 2 * len(SHAPES)


async def test_a_relink_that_changes_the_strength_moves_the_neighbour_between_walks(world):
    await world.docs("c", "d")
    await world.link("c", "d")
    assert world.names(await world.walk("c", edge_type="strong")) == {"c", "d"}
    assert world.names(await world.walk("c", edge_type="weak")) == {"c"}

    await world.link("c", "d", edge_type="weak")

    assert world.names(await world.walk("c", edge_type="strong")) == {"c"}
    assert world.names(await world.walk("c", edge_type="weak")) == {"c", "d"}


async def test_a_strong_walk_costs_the_same_round_trips_as_the_unfiltered_one(world, monkeypatch):
    await _mixed(world)
    statements: list[str] = []

    class _Recording:
        def __init__(self, conn) -> None:
            self._conn = conn

        def __getattr__(self, name):
            attr = getattr(self._conn, name)
            if name != "fetch":
                return attr

            async def fetch(query, *args, **kwargs):
                statements.append(query)
                return await attr(query, *args, **kwargs)

            return fetch

    class _Acquire:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def __aenter__(self):
            return _Recording(await self._inner.__aenter__())

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

    class _Pool:
        def acquire(self):
            return _Acquire(world.pool.acquire())

    async def _get_pool():
        return _Pool()

    monkeypatch.setattr(kg_service, "get_pool", _get_pool)

    await world.walk("c")
    unfiltered = len(statements)
    statements.clear()
    await world.walk("c", edge_type="strong")

    assert len(statements) == unfiltered


# ── Direction: an outward walk stops at shared context ───────────────────


async def _incidents(world: World) -> None:
    """An incident, the cluster it ran on, a sibling incident on that same cluster,
    and a postmortem derived from the incident that also names a service.

    The incidents name the cluster as context (`related_to`), not as something they
    depend on: an incoming `depends_on` is followed, so a cluster that incidents
    *depended on* would be walked through to every one of them.
    """
    await world.docs("incident", "other-incident", "cluster", "pm", "service")
    await world.link("incident", "cluster", "related_to")
    await world.link("other-incident", "cluster", "related_to")
    await world.link("pm", "incident", "derived_from")
    await world.link("pm", "service", "references")


async def test_an_outward_walk_follows_its_own_context_and_what_was_derived_from_it(world):
    await _incidents(world)

    graph = await world.walk("incident", direction="outward")

    assert world.names(graph) == {"incident", "cluster", "pm", "service"}
    assert "other-incident" not in world.names(graph)  # it only points at the cluster, shared context
    assert world.edge_names(graph) == {
        ("incident", "cluster", "related_to"),  # outgoing
        ("pm", "incident", "derived_from"),  # incoming, but derived_from
        ("pm", "service", "references"),  # the postmortem's own outgoing edge, one hop further out
    }
    assert {n["uri"].rsplit("/", 1)[-1].removesuffix(".md"): n["depth"] for n in graph["nodes"]} == {
        "incident": 0, "cluster": 1, "pm": 1, "service": 2,
    }
    assert graph["truncated"] is False


@pytest.mark.parametrize("direction", [None, "both"])
async def test_both_is_the_walk_it_always_was(world, direction):
    await _incidents(world)

    graph = await world.walk("incident", direction=direction)
    omitted = await world.walk("incident")

    assert world.names(graph) == {"incident", "cluster", "pm", "other-incident", "service"}
    assert len(graph["edges"]) == 4 and graph["truncated"] is False
    assert world.edge_names(graph) == world.edge_names(omitted) and world.names(graph) == world.names(omitted)


async def test_an_outward_walk_respects_hops(world):
    await _incidents(world)

    graph = await world.walk("incident", hops=1, direction="outward")

    assert world.names(graph) == {"incident", "cluster", "pm"}  # the postmortem's edge to the service is a second hop
    assert world.edge_names(graph) == {("incident", "cluster", "related_to"), ("pm", "incident", "derived_from")}
    assert graph["truncated"] is False  # the radius is not a cut


async def test_started_on_a_hub_an_outward_walk_returns_the_hub_alone(world):
    await _incidents(world)

    outward = await world.walk("cluster", direction="outward")
    everything = await world.walk("cluster")

    assert world.names(outward) == {"cluster"} and outward["edges"] == []
    assert world.names(everything) == {"cluster", "incident", "other-incident", "pm"}


async def test_only_derived_from_and_depends_on_are_followed_against_the_arrow_for_every_relation(world):
    """The real SQL over every relation `akb_link` writes, and the implicit body link."""
    relations = [*LINK_RELATION_TYPES]
    await world.docs("c", "body-in", *[f"{r}-in" for r in relations], *[f"{r}-out" for r in relations])
    for relation in relations:
        await world.link(f"{relation}-in", "c", relation)  # points at c
        await world.link("c", f"{relation}-out", relation)  # c points at it
    await world.extract("body-in", body=f"See [c]({world.uri('c')}).")  # implicit links_to, points at c

    outward = await world.walk("c", hops=1, direction="outward")
    both = await world.walk("c", hops=1)

    # Written out, not read from the constant: what was derived from c and what depends on it.
    assert set(OUTWARD_INCOMING_RELATIONS) == {"derived_from", "depends_on"}
    assert world.names(outward) == {"c", "derived_from-in", "depends_on-in", *[f"{r}-out" for r in relations]}
    # related_to, references, implements, attached_to and the body link (links_to) are not followed.
    assert world.names(both) == world.names(outward) | {
        f"{r}-in" for r in relations if r not in OUTWARD_INCOMING_RELATIONS
    } | {"body-in"}
    assert {"related_to-in", "references-in", "body-in"}.isdisjoint(world.names(outward))


async def test_derived_from_is_followed_against_the_arrow_from_every_reached_node(world):
    await world.docs("incident", "pm", "summary", "raw-log")
    await world.link("pm", "incident", "derived_from")
    await world.link("summary", "pm", "derived_from")  # derived from the postmortem, not from the incident
    await world.link("incident", "raw-log", "derived_from")  # the incident derives from a log: outgoing

    graph = await world.walk("incident", direction="outward")

    assert world.names(graph) == {"incident", "pm", "summary", "raw-log"}
    assert len(graph["edges"]) == 3


async def test_outward_composes_with_edge_type(world):
    await world.docs("incident", "cluster", "owner", "pm", "draft", "log", "sibling")
    await world.link("incident", "cluster", "related_to")  # strong, outgoing
    await world.link("incident", "owner", "references", edge_type="weak")  # weak, outgoing
    await world.link("pm", "incident", "derived_from")  # strong, derived from it
    await world.link("draft", "incident", "derived_from", edge_type="weak")  # weak, derived from it
    await world.seed_edge("log", "incident", kind="implicit", relation="derived_from", metadata=None)  # implicit: weak
    await world.link("sibling", "cluster", "related_to")  # strong, but shared context

    outward = await world.walk("incident", direction="outward")
    strong = await world.walk("incident", edge_type="strong", direction="outward")
    weak = await world.walk("incident", edge_type="weak", direction="outward")
    strong_both = await world.walk("incident", edge_type="strong")

    assert world.names(outward) == {"incident", "cluster", "owner", "pm", "draft", "log"}
    assert world.names(strong) == {"incident", "cluster", "pm"}
    assert {e["edge_type"] for e in strong["edges"]} == {"strong"}
    assert world.names(weak) == {"incident", "owner", "draft", "log"}
    assert {e["edge_type"] for e in weak["edges"]} == {"weak"}
    assert {e["kind"] for e in weak["edges"]} == {"explicit", "implicit"}
    # Each narrows what the other leaves: without `outward` the strong walk reaches the sibling.
    assert world.names(strong_both) == {"incident", "cluster", "pm", "sibling"}


async def _services(world: World) -> None:
    """An incident that names a service, what depends on that service and on those, and the
    documents that merely point at it. web and batch depend on api, mobile depends on web
    (not on api itself), and api depends on db."""
    await world.docs("incident", "api", "web", "mobile", "batch", "db", "runbook", "notes", "sibling")
    await world.link("incident", "api", "related_to")  # the service it names
    await world.link("web", "api", "depends_on")  # web depends on api: affected by it
    await world.link("mobile", "web", "depends_on")  # mobile depends on web: one level further
    await world.link("batch", "api", "depends_on", edge_type="weak")  # a weak dependent
    await world.link("api", "db", "depends_on")  # api depends on db: outgoing, always followed
    await world.link("runbook", "api", "references")  # points at api, depends on nothing
    await world.extract("notes", body=f"See [api]({world.uri('api')}).")  # a body link: implicit, weak
    await world.link("sibling", "api", "related_to")  # another incident on the same service


async def test_an_outward_walk_reaches_what_depends_on_a_service_a_level_per_hop(world):
    await _services(world)

    one = await world.walk("incident", hops=1, direction="outward")
    two = await world.walk("incident", hops=2, direction="outward")
    three = await world.walk("incident", hops=3, direction="outward")

    assert world.names(one) == {"incident", "api"}
    # Two hops out: api's dependents (web, batch) against the arrow, and what api depends on (db) with it.
    assert world.names(two) == {"incident", "api", "db", "web", "batch"}
    assert world.edge_names(two) == {
        ("incident", "api", "related_to"), ("api", "db", "depends_on"),
        ("web", "api", "depends_on"), ("batch", "api", "depends_on"),
    }
    # Three hops out: the dependents of a dependent.
    assert world.names(three) == {"incident", "api", "db", "web", "batch", "mobile"}
    assert world.edge_names(three) == world.edge_names(two) | {("mobile", "web", "depends_on")}
    assert {n["uri"].rsplit("/", 1)[-1].removesuffix(".md"): n["depth"] for n in three["nodes"]} == {
        "incident": 0, "api": 1, "db": 2, "web": 2, "batch": 2, "mobile": 3,
    }
    # ... and none of what merely points at the service: related_to, references, a body link.
    assert {"runbook", "notes", "sibling"}.isdisjoint(world.names(three))
    assert one["truncated"] is two["truncated"] is three["truncated"] is False


@pytest.mark.parametrize("direction", [None, "both"])
async def test_both_takes_the_dependents_and_everything_else_that_points_at_the_service(world, direction):
    await _services(world)

    graph = await world.walk("incident", hops=3, direction=direction)

    assert world.names(graph) == {"incident", "api", "db", "web", "batch", "mobile", "runbook", "notes", "sibling"}
    assert len(graph["edges"]) == 8 and graph["truncated"] is False


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
async def test_outward_dependents_compose_with_edge_type(world, edge_type, start, expected):
    await _services(world)

    graph = await world.walk(start, hops=3, edge_type=edge_type, direction="outward")

    assert world.names(graph) == expected
    if edge_type:
        assert {e["edge_type"] for e in graph["edges"]} <= {edge_type}


async def test_a_service_document_listing_depends_on_in_its_frontmatter_is_a_dependent_too(world):
    """A document's frontmatter `depends_on:` list leaves an implicit edge, which is weak."""
    await world.docs("api", "web", "mobile", "db", "wiki")
    await world.extract_frontmatter("web", depends_on=("api",))
    await world.extract_frontmatter("mobile", depends_on=("web",))
    await world.extract_frontmatter("api", depends_on=("db",))
    await world.extract_frontmatter("wiki", related_to=("api",))  # frontmatter related_to: not a dependency

    every = await world.walk("api", hops=3, direction="outward")
    weak = await world.walk("api", hops=3, edge_type="weak", direction="outward")
    strong = await world.walk("api", hops=3, edge_type="strong", direction="outward")
    both = await world.walk("api", hops=3)

    assert world.names(every) == world.names(weak) == {"api", "db", "web", "mobile"}
    assert {(e["kind"], e["edge_type"], e["relation"]) for e in every["edges"]} == {("implicit", "weak", "depends_on")}
    assert world.names(strong) == {"api"}  # an implicit edge is weak
    assert world.names(both) == {"api", "db", "web", "mobile", "wiki"}


async def test_a_service_everything_depends_on_is_walked_through_and_the_cap_still_applies(world):
    """The other side of the hub test: a dependency hub is not stopped at, and `limit` bounds it."""
    await world.docs("core")
    await world.seed_many([f"service-{i}" for i in range(250)], "core", "depends_on")

    graph = await world.walk("core", hops=1, limit=200, direction="outward")

    assert len(graph["nodes"]) == 200 and graph["truncated"] is True  # core and 199 of its 250 dependents
    assert all(e["target"] == world.uri("core") for e in graph["edges"])


async def test_an_implicit_outgoing_link_is_followed_and_an_implicit_incoming_one_is_not(world):
    await world.docs("a", "b")
    await world.extract("a", body=f"See [b]({world.uri('b')}).")  # a --links_to--> b, implicit

    from_a = await world.walk("a", direction="outward")
    from_b = await world.walk("b", direction="outward")
    weak_from_a = await world.walk("a", edge_type="weak", direction="outward")
    strong_from_a = await world.walk("a", edge_type="strong", direction="outward")

    assert world.names(from_a) == {"a", "b"} and world.names(weak_from_a) == {"a", "b"}
    assert world.names(strong_from_a) == {"a"}  # an implicit edge is weak
    assert world.names(from_b) == {"b"}  # the link is incoming at b, and not derived_from


async def test_an_edge_the_walk_did_not_follow_is_not_emitted_even_between_visited_nodes(world):
    """x points at y and both are reached. x sits on the radius, so its own edges are
    never read, and y only takes an incoming edge when it is `derived_from`."""
    await world.docs("s", "y", "p", "x")
    await world.link("s", "y")
    await world.link("s", "p")
    await world.link("p", "x")
    await world.link("x", "y")

    both = await world.walk("s")
    outward = await world.walk("s", direction="outward")

    assert world.names(both) == world.names(outward) == {"s", "y", "p", "x"}
    assert len(both["edges"]) == 4
    assert world.edge_names(outward) == world.edge_names(both) - {("x", "y", "related_to")}


async def test_a_hub_does_not_spend_the_node_cap_on_an_outward_walk(world):
    """The shape seen on real data: every incident points at one cluster."""
    await world.docs("cluster")
    await world.seed_many([f"incident-{i}" for i in range(250)], "cluster", "related_to")

    fan_out = await world.walk("incident-0", limit=200, edge_type="strong")
    outward = await world.walk("incident-0", limit=200, edge_type="strong", direction="outward")

    assert len(fan_out["nodes"]) == 200 and fan_out["truncated"] is True  # incident, cluster, then the others
    assert world.names(outward) == {"incident-0", "cluster"} and len(outward["edges"]) == 1
    assert outward["truncated"] is False


@pytest.mark.parametrize(
    ("limit", "kept", "truncated"),
    [(50, 11, False), (11, 11, False), (10, 10, True), (3, 3, True), (1, 1, True)],
)
async def test_truncated_means_the_same_for_an_outward_walk(world, limit, kept, truncated):
    await _hub(world, strong=10, weak=0)  # ten outgoing neighbours

    graph = await world.walk("hub", hops=1, limit=limit, direction="outward")

    assert len(graph["nodes"]) == kept and graph["truncated"] is truncated
    assert all(e["source"] in {n["uri"] for n in graph["nodes"]} for e in graph["edges"])  # no stub endpoints


async def test_neighbours_an_outward_walk_does_not_follow_are_not_a_cut(world):
    await world.docs("c")
    await world.seed_many([f"leaf-{i}" for i in range(5)], "c", "references")  # five documents point at c

    outward = await world.walk("c", hops=1, limit=1, direction="outward")
    default = await world.walk("c", hops=1, limit=1)

    assert world.names(outward) == {"c"} and outward["edges"] == [] and outward["truncated"] is False
    assert len(default["nodes"]) == 1 and default["truncated"] is True


async def test_an_outward_walk_costs_the_same_round_trips_as_the_default_one(world, monkeypatch):
    await _incidents(world)
    statements: list[str] = []

    class _Recording:
        def __init__(self, conn) -> None:
            self._conn = conn

        def __getattr__(self, name):
            attr = getattr(self._conn, name)
            if name != "fetch":
                return attr

            async def fetch(query, *args, **kwargs):
                statements.append(query)
                return await attr(query, *args, **kwargs)

            return fetch

    class _Acquire:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def __aenter__(self):
            return _Recording(await self._inner.__aenter__())

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

    class _Pool:
        def acquire(self):
            return _Acquire(world.pool.acquire())

    async def _get_pool():
        return _Pool()

    monkeypatch.setattr(kg_service, "get_pool", _get_pool)

    await world.walk("incident")
    default = len(statements)
    statements.clear()
    await world.walk("incident", direction="outward")

    assert len(statements) == default  # two fetches a wave, three waves, either way


async def test_the_service_validates_direction_before_it_touches_a_row(world):
    await _incidents(world)

    invalid = await kg_service.get_graph(world.vault, world.uri("incident"), vault_id=world.vault_id, direction="sideways")
    refused = await kg_service.get_graph(world.vault, None, vault_id=world.vault_id, direction="outward")
    overview = await kg_service.get_graph(world.vault, None, vault_id=world.vault_id, direction="both")

    assert invalid["code"] == "invalid_argument" and "nodes" not in invalid
    assert refused["code"] == "invalid_argument" and "`uri`" in refused["error"] and "nodes" not in refused
    assert len(overview["edges"]) == 4  # `both` is the overview it always was


# ── The overview is not strength-aware, and says so ──────────────────────


async def test_the_overview_still_returns_every_edge_and_refuses_edge_type_through_get_graph(world):
    await _mixed(world)

    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id, include_orphans=False)
    whole_vault = await kg_service.get_graph(world.vault, None, vault_id=world.vault_id)
    refused = await kg_service.get_graph(world.vault, None, vault_id=world.vault_id, edge_type="strong")

    assert {e["edge_type"] for e in overview["edges"]} == {"strong", "weak"}
    assert len(overview["edges"]) == 7 and overview["truncated"] is False
    assert {e["edge_type"] for e in whole_vault["edges"]} == {"strong", "weak"}
    assert refused["code"] == "invalid_argument" and "nodes" not in refused


# ── REST and MCP, end to end ─────────────────────────────────────────────


class _FakeUser:
    user_id = "u-test"
    username = "tester"


@pytest_asyncio.fixture
async def rest(world, monkeypatch):
    async def _reader(*_args, **_kwargs):
        return {"vault_id": world.vault_id, "role": "reader", "status": "active"}

    monkeypatch.setattr(knowledge, "check_vault_access", _reader)
    app = FastAPI()
    app.include_router(knowledge.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_rest_walks_only_the_requested_strength(world, rest):
    await _mixed(world)
    params = {"uri": world.uri("c"), "hops": 2, "limit": 50}

    default = (await rest.get("/api/v1/graph", params=params)).json()
    strong = (await rest.get("/api/v1/graph", params={**params, "edge_type": "strong"})).json()
    weak = (await rest.get("/api/v1/graph", params={**params, "edge_type": "weak"})).json()

    assert default["kind"] == strong["kind"] == weak["kind"] == "graph_neighbors"
    assert world.names(strong) == {"c", "s-out", "s-in", "s-out-2"}
    assert world.names(weak) == {"c", "w-out", "w-in"}
    assert len(world.names(default)) == 8
    assert default["truncated"] is False and strong["truncated"] is False


async def test_rest_reports_truncation_and_validates_the_parameter(world, rest):
    await _hub(world, strong=10, weak=30)
    params = {"uri": world.uri("hub"), "hops": 1}

    cut = (await rest.get("/api/v1/graph", params={**params, "limit": 5, "edge_type": "strong"})).json()
    fits = (await rest.get("/api/v1/graph", params={**params, "limit": 11, "edge_type": "strong"})).json()
    invalid = await rest.get("/api/v1/graph", params={**params, "edge_type": "medium"})
    overview = await rest.get("/api/v1/graph", params={"vault": world.vault, "edge_type": "strong"})

    assert len(cut["nodes"]) == 5 and cut["truncated"] is True
    assert len(fits["nodes"]) == 11 and fits["truncated"] is False
    assert invalid.status_code == 422
    assert overview.status_code == 400 and "`uri`" in overview.json()["detail"]


@pytest_asyncio.fixture
async def mcp(world, monkeypatch):
    async def _access(*_args, **_kwargs):
        return {"vault_id": world.vault_id, "role": "reader"}

    monkeypatch.setattr(server, "check_vault_access", _access)
    user = server._MCPUser(user_id=str(uuid.uuid4()), username="tester")

    async def call(tool: str, **arguments: Any) -> dict:
        return await server._dispatch(tool, arguments, user)

    return call


async def test_mcp_akb_graph_walks_only_the_requested_strength(world, mcp):
    await _mixed(world)
    uri = world.uri("c")

    default = await mcp("akb_graph", uri=uri)
    strong = await mcp("akb_graph", uri=uri, edge_type="strong")
    weak = await mcp("akb_graph", uri=uri, edge_type="weak")
    explicit_null = await mcp("akb_graph", uri=uri, edge_type=None)

    assert len(world.names(default)) == 8 and world.names(explicit_null) == world.names(default)
    assert world.names(strong) == {"c", "s-out", "s-in", "s-out-2"}
    assert world.names(weak) == {"c", "w-out", "w-in"}
    assert {e["edge_type"] for e in strong["edges"]} == {"strong"}
    assert strong["truncated"] is False


async def test_mcp_akb_graph_refuses_what_the_walk_cannot_honour(world, mcp):
    await _mixed(world)

    invalid = await mcp("akb_graph", uri=world.uri("c"), edge_type="medium")
    overview = await mcp("akb_graph", vault=world.vault, edge_type="strong")
    plain_overview = await mcp("akb_graph", vault=world.vault)

    assert invalid["code"] == "invalid_argument"
    assert overview["code"] == "invalid_argument" and overview["hint"]
    assert "nodes" not in invalid and "nodes" not in overview
    assert {e["edge_type"] for e in plain_overview["edges"]} == {"strong", "weak"}  # untouched


async def test_rest_walks_outward_and_validates_the_parameter(world, rest):
    await _incidents(world)
    params = {"uri": world.uri("incident"), "hops": 2, "limit": 50}

    default = (await rest.get("/api/v1/graph", params=params)).json()
    outward = (await rest.get("/api/v1/graph", params={**params, "direction": "outward"})).json()
    both = (await rest.get("/api/v1/graph", params={**params, "direction": "both"})).json()
    composed = (await rest.get("/api/v1/graph", params={**params, "direction": "outward", "edge_type": "weak"})).json()
    invalid = await rest.get("/api/v1/graph", params={**params, "direction": "sideways"})
    refused = await rest.get("/api/v1/graph", params={"vault": world.vault, "direction": "outward"})
    overview = await rest.get("/api/v1/graph", params={"vault": world.vault, "direction": "both"})

    assert default["kind"] == outward["kind"] == "graph_neighbors"
    assert world.names(outward) == {"incident", "cluster", "pm", "service"}
    assert world.names(both) == world.names(default) == {"incident", "cluster", "pm", "other-incident", "service"}
    assert world.names(composed) == {"incident"}  # every edge here is strong
    assert outward["truncated"] is False
    assert invalid.status_code == 422
    assert refused.status_code == 400 and "`uri`" in refused.json()["detail"]
    assert overview.status_code == 200 and overview.json()["kind"] == "graph_overview"


async def test_mcp_akb_graph_walks_outward_and_validates_the_parameter(world, mcp):
    await _incidents(world)
    uri = world.uri("incident")

    default = await mcp("akb_graph", uri=uri)
    outward = await mcp("akb_graph", uri=uri, direction="outward")
    explicit_null = await mcp("akb_graph", uri=uri, direction=None)
    strong_outward = await mcp("akb_graph", uri=uri, direction="outward", edge_type="strong")
    invalid = await mcp("akb_graph", uri=uri, direction="sideways")
    refused = await mcp("akb_graph", vault=world.vault, direction="outward")
    overview = await mcp("akb_graph", vault=world.vault, direction="both")

    assert world.names(outward) == {"incident", "cluster", "pm", "service"}
    assert world.names(explicit_null) == world.names(default) == {"incident", "cluster", "pm", "other-incident", "service"}
    assert world.names(strong_outward) == {"incident", "cluster", "pm", "service"}
    assert {e["edge_type"] for e in outward["edges"]} == {"strong"}
    assert invalid["code"] == "invalid_argument" and "nodes" not in invalid
    assert refused["code"] == "invalid_argument" and refused["hint"] and "nodes" not in refused
    assert len(overview["edges"]) == 4  # the vault graph, untouched


async def test_rest_and_mcp_walk_outward_to_what_depends_on_a_service(world, rest, mcp):
    await _services(world)
    start = world.uri("incident")

    over_rest = (await rest.get("/api/v1/graph", params={"uri": start, "hops": 3, "direction": "outward"})).json()
    over_mcp = await mcp("akb_graph", uri=start, hops=3, direction="outward")
    strong_over_mcp = await mcp("akb_graph", uri=start, hops=3, direction="outward", edge_type="strong")

    assert over_rest["kind"] == "graph_neighbors"
    assert world.names(over_rest) == world.names(over_mcp) == {"incident", "api", "db", "web", "batch", "mobile"}
    assert world.edge_names(over_rest) == world.edge_names(over_mcp)
    assert world.names(strong_over_mcp) == {"incident", "api", "db", "web", "mobile"}  # batch is weak
    assert over_rest["truncated"] is False and over_mcp["truncated"] is False
