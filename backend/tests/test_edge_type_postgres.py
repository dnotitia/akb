"""Real-PostgreSQL regressions for ``edge_type`` (strong | weak) on relations.

The DB-free half lives in ``test_edge_type_unit.py``. What only a database can
settle is here:

* an explicit edge keeps its strength in ``edges.metadata->>'edge_type'`` — no
  column, no migration — and linking the same edge again updates it **in place**
  and **merges** with the caller's metadata instead of replacing it;
* a re-link that names no ``edge_type`` (a pre-``edge_type`` REST client, an older
  MCP proxy, a caller updating only metadata) **keeps the stored strength** and
  never promotes a weak edge; a new edge is strong; a named value always sets it;
* every ``akb_link`` edge that predates the attribute (no key, a NULL or
  non-object ``metadata``, a value outside the vocabulary) reads as ``strong``
  with no backfill;
* implicit edges are always ``weak`` — derived, so never stored — and a document
  rewrite neither demotes nor drops an explicit edge's strength;
* ``akb_relations`` / ``akb_graph`` / ``akb_provenance``, the graph overview, and
  the REST routes that serve the same rows all carry ``edge_type`` per edge.

Set ``AKB_TEST_DSN`` to a PostgreSQL 16 + pgvector instance; ``REQUIRE_REAL_PG=1``
turns a missing instance from a skip into a failure.
"""

from __future__ import annotations

import json
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
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-edge-type-pg-")

from app.api.deps import get_current_user
from app.api.routes import knowledge
from app.services import kg_service
from app.services.kg_service import store_document_relations
from app.services.uri_service import doc_uri
from mcp_server import server


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)

A, B, C, D = "specs/a.md", "specs/b.md", "specs/c.md", "specs/d.md"


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

    def uri(self, path: str) -> str:
        return doc_uri(self.vault, path)

    async def link(self, source: str, target: str, relation: str = "related_to", **kwargs: Any) -> dict:
        return await kg_service.link_resources(
            self.vault, self.uri(source), self.uri(target), relation, **kwargs,
        )

    async def relations(self, path: str, **kwargs: Any) -> list[dict]:
        return await kg_service.get_resource_relations(
            self.vault, self.uri(path), vault_id=self.vault_id, **kwargs,
        )

    async def edge_rows(self) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT source_uri, target_uri, relation_type, kind, metadata, metadata->>'edge_type' AS stored "
                "FROM edges WHERE vault_id = $1 ORDER BY relation_type, source_uri, target_uri",
                self.vault_id,
            )

    async def only_edge(self) -> asyncpg.Record:
        (row,) = await self.edge_rows()
        return row

    async def metadata(self) -> dict:
        return json.loads((await self.only_edge())["metadata"])

    async def seed_edge(self, source: str, target: str, *, relation: str = "related_to",
                        kind: str = "explicit", metadata: str | None = "{}") -> None:
        """Insert a row exactly as an older release (or a bare writer) would have."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO edges (vault_id, source_uri, target_uri, relation_type, source_type, target_type, "
                "metadata, kind) VALUES ($1, $2, $3, $4, 'doc', 'doc', $5::jsonb, $6)",
                self.vault_id, self.uri(source), self.uri(target), relation, metadata, kind,
            )

    async def extract(self, source: str, *, related_to: list[str] = (), body: str = "") -> int:
        """Run the implicit extraction a document write runs for ``source``."""
        async with self.pool.acquire() as conn:
            return await store_document_relations(
                conn, self.vault_id, self.vault, source, [], list(related_to), [], body,
            )


@pytest_asyncio.fixture
async def world(pool, monkeypatch):
    name = f"_test_edge_type_{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        vault_id = await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            name, f"/tmp/{name}.git",
        )
        for path in (A, B, C, D):
            await conn.execute(
                "INSERT INTO documents (id, vault_id, path, title) VALUES ($1, $2, $3, $4)",
                uuid.uuid4(), vault_id, path, f"Title {path}",
            )

    async def _get_pool():
        return pool

    monkeypatch.setattr(kg_service, "get_pool", _get_pool)
    try:
        yield World(pool=pool, vault=name, vault_id=vault_id)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)


def _by_relation(rows: list[dict]) -> dict[str, tuple[str, str]]:
    return {row["relation"]: (row["kind"], row["edge_type"]) for row in rows}


# ── Creating, reading and re-linking an explicit edge ────────────────────


async def test_a_link_without_edge_type_is_stored_strong_and_listed_strong(world):
    result = await world.link(A, B)

    assert result["edge_type"] == "strong"
    row = await world.only_edge()
    assert (row["kind"], row["stored"]) == ("explicit", "strong")
    assert _by_relation(await world.relations(A)) == {"related_to": ("explicit", "strong")}


async def test_a_weak_link_is_stored_weak_and_listed_weak_from_both_ends(world):
    await world.link(A, B, edge_type="weak")

    assert (await world.only_edge())["stored"] == "weak"
    assert _by_relation(await world.relations(A)) == {"related_to": ("explicit", "weak")}
    incoming = await world.relations(B, direction="incoming")
    assert [(r["direction"], r["edge_type"]) for r in incoming] == [("incoming", "weak")]


async def test_relinking_with_another_edge_type_updates_the_edge_in_place(world):
    await world.link(A, B)
    first = await world.only_edge()

    await world.link(A, B, edge_type="weak")
    assert (await world.only_edge())["stored"] == "weak"

    await world.link(A, B, edge_type="strong")
    last = await world.only_edge()  # `only_edge` also proves there is still exactly one row
    assert last["stored"] == "strong"
    assert (last["source_uri"], last["target_uri"], last["kind"]) == (
        first["source_uri"], first["target_uri"], "explicit",
    )


async def test_a_bare_relink_keeps_a_weak_edge_weak(world):
    """A caller that says nothing about strength (a client that predates `edge_type`,
    an older MCP proxy, one updating only metadata) must not promote the edge."""
    await world.link(A, B, edge_type="weak")

    result = await world.link(A, B)

    assert result["edge_type"] == "weak"  # the answer says what is stored, not what was assumed
    assert (await world.only_edge())["stored"] == "weak"
    assert _by_relation(await world.relations(A)) == {"related_to": ("explicit", "weak")}


async def test_a_bare_relink_keeps_a_strong_edge_strong(world):
    await world.link(A, B, edge_type="strong")

    result = await world.link(A, B)

    assert result["edge_type"] == "strong"
    assert (await world.only_edge())["stored"] == "strong"


async def test_an_explicit_value_still_sets_the_strength_either_way(world):
    await world.link(A, B, edge_type="weak")

    promoted = await world.link(A, B, edge_type="strong")
    assert promoted["edge_type"] == "strong"
    assert (await world.only_edge())["stored"] == "strong"

    demoted = await world.link(A, B, edge_type="weak")
    assert demoted["edge_type"] == "weak"
    assert (await world.only_edge())["stored"] == "weak"

    await world.link(A, B)  # and a bare re-link after either leaves it where it is
    assert (await world.only_edge())["stored"] == "weak"


async def test_a_new_edge_without_edge_type_is_strong_and_says_so(world):
    result = await world.link(A, B, "references")

    assert result["edge_type"] == "strong"
    assert (await world.only_edge())["stored"] == "strong"  # written, not left to the reader's default


@pytest.mark.parametrize(
    ("kind", "metadata", "expected"),
    [
        # An explicit edge keeps the strength it reads as ...
        pytest.param("explicit", '{"edge_type": "weak"}', "weak", id="explicit-weak"),
        pytest.param("explicit", '{"edge_type": "strong"}', "strong", id="explicit-strong"),
        # ... so every row that reads as strong stays strong, and is now stated.
        pytest.param("explicit", "{}", "strong", id="explicit-empty-object"),
        pytest.param("explicit", None, "strong", id="explicit-sql-null"),
        pytest.param("explicit", "[]", "strong", id="explicit-array"),
        pytest.param("explicit", '"weak"', "strong", id="explicit-string-scalar"),
        pytest.param("explicit", '{"note": "x"}', "strong", id="explicit-other-keys-only"),
        pytest.param("explicit", '{"edge_type": "medium"}', "strong", id="explicit-outside-the-vocabulary"),
        pytest.param("explicit", '{"edge_type": "Weak"}', "strong", id="explicit-wrong-case"),
        # An implicit row has no stored strength (it is derived, always weak), so
        # linking over it makes a new explicit edge; a stray key means nothing.
        pytest.param("implicit", "{}", "strong", id="implicit"),
        pytest.param("implicit", '{"edge_type": "weak"}', "strong", id="implicit-stray-weak-key"),
    ],
)
async def test_a_bare_link_over_any_stored_row_keeps_what_the_row_reads_as(world, kind, metadata, expected):
    await world.seed_edge(A, B, kind=kind, metadata=metadata)

    result = await world.link(A, B)

    assert result["edge_type"] == expected
    row = await world.only_edge()
    assert (row["kind"], row["stored"]) == ("explicit", expected)
    assert _by_relation(await world.relations(A)) == {"related_to": ("explicit", expected)}


async def test_a_bare_relink_that_only_adds_metadata_cannot_promote_the_edge(world):
    await world.link(A, B, metadata={"note": "n"}, edge_type="weak")

    await world.link(A, B, metadata={"extra": 1})

    assert await world.metadata() == {"note": "n", "extra": 1, "edge_type": "weak"}


async def test_the_edge_type_is_per_relation_not_per_pair(world):
    await world.link(A, B, "related_to", edge_type="weak")
    await world.link(A, B, "references")

    assert _by_relation(await world.relations(A)) == {
        "related_to": ("explicit", "weak"),
        "references": ("explicit", "strong"),
    }


async def test_relinking_merges_the_callers_metadata_with_the_edge_type(world):
    await world.link(A, B, metadata={"note": "n"}, edge_type="weak")
    assert await world.metadata() == {"note": "n", "edge_type": "weak"}

    # A re-link that only changes the strength keeps the caller's other keys ...
    await world.link(A, B, edge_type="strong")
    assert await world.metadata() == {"note": "n", "edge_type": "strong"}

    # ... a re-link that only sends metadata cannot drop the strength ...
    await world.link(A, B, metadata={"extra": 1}, edge_type="strong")
    assert await world.metadata() == {"note": "n", "extra": 1, "edge_type": "strong"}

    # ... and a key sent again takes the new value.
    await world.link(A, B, metadata={"note": "m"}, edge_type="weak")
    assert await world.metadata() == {"note": "m", "extra": 1, "edge_type": "weak"}


@pytest.mark.parametrize(
    ("metadata", "edge_type", "expected"),
    [({"edge_type": "weak"}, "strong", "strong"), ({"edge_type": "strong"}, "weak", "weak")],
)
async def test_the_reserved_key_cannot_be_smuggled_through_metadata(world, metadata, edge_type, expected):
    await world.link(A, B, metadata=metadata, edge_type=edge_type)

    assert (await world.only_edge())["stored"] == expected
    assert _by_relation(await world.relations(A)) == {"related_to": ("explicit", expected)}


@pytest.mark.parametrize("smuggled", ["weak", "strong", "medium", None])
async def test_the_reserved_key_is_ignored_when_no_edge_type_is_named_too(world, smuggled):
    """Naming nothing keeps the stored strength (strong for a new edge); the key
    inside `metadata` is still not a way to set it."""
    await world.link(A, B, metadata={"edge_type": smuggled})
    assert (await world.only_edge())["stored"] == "strong"

    await world.link(A, B, edge_type="weak")
    await world.link(A, B, metadata={"edge_type": smuggled})
    assert (await world.only_edge())["stored"] == "weak"


async def test_an_invalid_edge_type_writes_nothing(world):
    result = await world.link(A, B, edge_type="medium")

    assert result["code"] == "invalid_argument"
    assert await world.edge_rows() == []


# ── Rows that predate the attribute ──────────────────────────────────────


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param("{}", id="empty-object"),
        pytest.param(None, id="sql-null"),
        pytest.param('{"note": "x"}', id="other-keys-only"),
        pytest.param("[]", id="array"),
        pytest.param('"weak"', id="string-scalar"),
        pytest.param("null", id="json-null"),
        pytest.param('{"edge_type": null}', id="key-is-json-null"),
        pytest.param('{"edge_type": "medium"}', id="outside-the-vocabulary"),
        pytest.param('{"edge_type": "Weak"}', id="wrong-case"),
    ],
)
async def test_an_explicit_edge_without_a_valid_key_reads_strong_everywhere(world, metadata):
    await world.seed_edge(A, B, metadata=metadata)

    assert _by_relation(await world.relations(A)) == {"related_to": ("explicit", "strong")}
    graph = await kg_service.get_graph(world.vault, world.uri(A), hops=1, vault_id=world.vault_id)
    assert [(e["kind"], e["edge_type"]) for e in graph["edges"]] == [("explicit", "strong")]
    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id, include_orphans=False)
    assert [(e["kind"], e["edge_type"]) for e in overview["edges"]] == [("explicit", "strong")]


@pytest.mark.parametrize("metadata", [None, "[]", '"weak"', "null"])
async def test_relinking_a_row_with_unusable_metadata_recovers_it(world, metadata):
    await world.seed_edge(A, B, metadata=metadata)

    await world.link(A, B, metadata={"note": "n"}, edge_type="weak")

    assert await world.metadata() == {"note": "n", "edge_type": "weak"}


async def test_a_legacy_weak_looking_key_is_honoured_only_on_an_explicit_edge(world):
    await world.seed_edge(A, B, metadata='{"edge_type": "weak"}')
    await world.seed_edge(A, C, relation="links_to", kind="implicit", metadata='{"edge_type": "strong"}')

    rows = {row["uri"]: (row["kind"], row["edge_type"]) for row in await world.relations(A)}

    assert rows == {
        world.uri(B): ("explicit", "weak"),
        world.uri(C): ("implicit", "weak"),  # a stray key never promotes a derived edge
    }


# ── Implicit edges ───────────────────────────────────────────────────────


async def test_implicit_edges_are_weak_and_the_value_is_never_stored(world):
    stored = await world.extract(A, related_to=[B], body=f"See [c]({C}).")

    assert stored == 2
    assert _by_relation(await world.relations(A)) == {
        "related_to": ("implicit", "weak"),
        "links_to": ("implicit", "weak"),
    }
    assert {row["stored"] for row in await world.edge_rows()} == {None}, "implicit strength is computed, not stored"


async def test_an_explicit_edge_keeps_its_edge_type_through_an_implicit_rewrite(world):
    await world.link(A, B, "related_to", edge_type="weak")
    await world.link(A, C, "references")

    # The same relation to the same target, declared again in frontmatter, plus
    # a body link: neither touches the explicit rows.
    await world.extract(A, related_to=[B], body=f"See [d]({D}).")
    await world.extract(A, related_to=[B], body=f"See [d]({D}).")

    assert _by_relation(await world.relations(A, relation_type="related_to")) == {"related_to": ("explicit", "weak")}
    assert _by_relation(await world.relations(A, relation_type="references")) == {"references": ("explicit", "strong")}
    assert _by_relation(await world.relations(A, relation_type="links_to")) == {"links_to": ("implicit", "weak")}


async def test_linking_over_an_implicit_edge_makes_it_explicit_and_strong(world):
    await world.extract(A, related_to=[B])
    assert _by_relation(await world.relations(A)) == {"related_to": ("implicit", "weak")}

    await world.link(A, B, "related_to")

    assert _by_relation(await world.relations(A)) == {"related_to": ("explicit", "strong")}
    await world.extract(A, related_to=[B])  # and a rewrite leaves it that way
    assert _by_relation(await world.relations(A)) == {"related_to": ("explicit", "strong")}


async def test_unlink_removes_an_explicit_edge_whatever_its_edge_type(world):
    await world.link(A, B, edge_type="weak")

    result = await kg_service.unlink_resources(
        world.uri(A), world.uri(B), relation_type="related_to", vault_id=world.vault_id,
    )

    assert result["unlinked"] == 1
    assert await world.edge_rows() == []


# ── Every listing, against real rows ─────────────────────────────────────


async def _mixed_edges(world: World) -> None:
    await world.link(A, B, "depends_on")  # explicit, strong
    await world.link(A, C, "references", edge_type="weak")  # explicit, weak
    await world.extract(A, body=f"See [d]({D}).")  # implicit links_to, weak


async def test_graph_neighbors_overview_and_provenance_carry_edge_type(world):
    await _mixed_edges(world)
    expected = {
        (world.uri(A), world.uri(B), "depends_on"): ("explicit", "strong"),
        (world.uri(A), world.uri(C), "references"): ("explicit", "weak"),
        (world.uri(A), world.uri(D), "links_to"): ("implicit", "weak"),
    }

    graph = await kg_service.get_graph(world.vault, world.uri(A), hops=1, vault_id=world.vault_id)
    assert {(e["source"], e["target"], e["relation"]): (e["kind"], e["edge_type"]) for e in graph["edges"]} == expected

    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id)
    assert {(e["source"], e["target"], e["relation"]): (e["kind"], e["edge_type"]) for e in overview["edges"]} == expected

    async with world.pool.acquire() as conn:
        doc_id = await conn.fetchval("SELECT id FROM documents WHERE vault_id = $1 AND path = $2", world.vault_id, A)
    provenance = await kg_service.get_provenance(str(doc_id), vault_id=world.vault_id)
    assert _by_relation(provenance["relations"]) == {
        "depends_on": ("explicit", "strong"),
        "references": ("explicit", "weak"),
        "links_to": ("implicit", "weak"),
    }


async def test_graph_neighbors_serve_the_edge_type_from_the_incoming_side_too(world):
    await world.link(B, A, "implements", edge_type="weak")

    graph = await kg_service.get_graph(world.vault, world.uri(A), hops=1, vault_id=world.vault_id)

    assert [(e["source"], e["target"], e["kind"], e["edge_type"]) for e in graph["edges"]] == [
        (world.uri(B), world.uri(A), "explicit", "weak"),
    ]


# ── REST, end to end ─────────────────────────────────────────────────────


class _FakeUser:
    user_id = "u-test"
    username = "tester"


@pytest_asyncio.fixture
async def rest(world, monkeypatch):
    async def _writer(*_args, **_kwargs):
        return {"vault_id": world.vault_id, "role": "writer", "status": "active"}

    monkeypatch.setattr(knowledge, "check_vault_access", _writer)
    app = FastAPI()
    app.include_router(knowledge.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_rest_link_graph_overview_and_relations_agree_on_edge_type(world, rest):
    body = {"source": world.uri(A), "target": world.uri(B), "relation": "related_to"}

    default = await rest.post("/api/v1/relations", json=body)
    assert default.status_code == 200, default.text
    assert (default.json()["kind"], default.json()["edge_type"]) == ("relation_link", "strong")

    weak = await rest.post("/api/v1/relations", json={**body, "edge_type": "weak", "metadata": {"note": "n"}})
    assert weak.status_code == 200, weak.text
    assert weak.json()["edge_type"] == "weak"
    assert await world.metadata() == {"note": "n", "edge_type": "weak"}

    relations = (await rest.get("/api/v1/relations", params={"uri": world.uri(A)})).json()["relations"]
    assert [(r["kind"], r["edge_type"]) for r in relations] == [("explicit", "weak")]

    graph = (await rest.get("/api/v1/graph", params={"uri": world.uri(A), "hops": 1})).json()
    assert graph["kind"] == "graph_neighbors"
    assert [(e["kind"], e["edge_type"]) for e in graph["edges"]] == [("explicit", "weak")]

    overview = (await rest.get("/api/v1/graph/overview", params={"vault": world.vault})).json()
    assert overview["kind"] == "graph_overview"
    assert [(e["kind"], e["edge_type"]) for e in overview["edges"]] == [("explicit", "weak")]

    whole_vault = (await rest.get("/api/v1/graph", params={"vault": world.vault})).json()
    assert [(e["kind"], e["edge_type"]) for e in whole_vault["edges"]] == [("explicit", "weak")]

    again = await rest.post("/api/v1/relations", json=body)  # a bare re-link leaves the strength alone
    assert again.json()["edge_type"] == "weak"
    explicit_null = await rest.post("/api/v1/relations", json={**body, "edge_type": None})  # null: not specified
    assert explicit_null.status_code == 200, explicit_null.text
    assert explicit_null.json()["edge_type"] == "weak"
    relations = (await rest.get("/api/v1/relations", params={"uri": world.uri(B), "direction": "incoming"})).json()
    assert [(r["direction"], r["edge_type"]) for r in relations["relations"]] == [("incoming", "weak")]

    strong = await rest.post("/api/v1/relations", json={**body, "edge_type": "strong"})  # a named value sets it
    assert strong.json()["edge_type"] == "strong"
    relations = (await rest.get("/api/v1/relations", params={"uri": world.uri(B), "direction": "incoming"})).json()
    assert [(r["direction"], r["edge_type"]) for r in relations["relations"]] == [("incoming", "strong")]
    assert await world.metadata() == {"note": "n", "edge_type": "strong"}  # and the caller's metadata survived all of it


async def test_rest_serves_a_row_that_predates_the_attribute_as_strong(world, rest):
    await world.seed_edge(A, B, metadata=None)

    relations = (await rest.get("/api/v1/relations", params={"uri": world.uri(A)})).json()["relations"]

    assert [(r["kind"], r["edge_type"]) for r in relations] == [("explicit", "strong")]


async def test_rest_rejects_an_invalid_edge_type_and_writes_nothing(world, rest):
    response = await rest.post(
        "/api/v1/relations",
        json={"source": world.uri(A), "target": world.uri(B), "relation": "related_to", "edge_type": "medium"},
    )

    assert response.status_code == 422
    assert await world.edge_rows() == []


# ── MCP, end to end ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def mcp(world, monkeypatch):
    async def _access(*_args, **_kwargs):
        return {"vault_id": world.vault_id, "role": "writer"}

    monkeypatch.setattr(server, "check_vault_access", _access)
    user = server._MCPUser(user_id=str(uuid.uuid4()), username="tester")

    async def call(tool: str, **arguments: Any) -> dict:
        return await server._dispatch(tool, arguments, user)

    return call


async def test_mcp_link_relations_and_graph_agree_on_edge_type(world, mcp):
    source, target = world.uri(A), world.uri(B)

    strong = await mcp("akb_link", source=source, target=target, relation="depends_on")
    assert strong["edge_type"] == "strong"
    weak = await mcp("akb_link", source=source, target=world.uri(C), relation="references", edge_type="weak")
    assert weak["edge_type"] == "weak"
    await world.extract(A, body=f"See [d]({D}).")

    relations = await mcp("akb_relations", uri=source)
    assert {(r["relation"], r["kind"], r["edge_type"]) for r in relations["relations"]} == {
        ("depends_on", "explicit", "strong"),
        ("references", "explicit", "weak"),
        ("links_to", "implicit", "weak"),
    }

    graph = await mcp("akb_graph", uri=source, hops=1)
    assert {(e["relation"], e["kind"], e["edge_type"]) for e in graph["edges"]} == {
        ("depends_on", "explicit", "strong"),
        ("references", "explicit", "weak"),
        ("links_to", "implicit", "weak"),
    }

    overview = await mcp("akb_graph", vault=world.vault)
    assert {(e["relation"], e["edge_type"]) for e in overview["edges"]} == {
        ("depends_on", "strong"), ("references", "weak"), ("links_to", "weak"),
    }

    relinked = await mcp("akb_link", source=source, target=target, relation="depends_on", edge_type="weak")
    assert relinked["edge_type"] == "weak"
    again = await mcp("akb_relations", uri=source, type="depends_on")
    assert [r["edge_type"] for r in again["relations"]] == ["weak"]

    # Saying nothing about strength (omitted, or null) leaves the weak edge weak ...
    for arguments in ({}, {"edge_type": None}):
        bare = await mcp("akb_link", source=source, target=target, relation="depends_on", **arguments)
        assert bare["edge_type"] == "weak", arguments
    assert [r["edge_type"] for r in (await mcp("akb_relations", uri=source, type="depends_on"))["relations"]] == ["weak"]
    # ... and naming it sets it.
    promoted = await mcp("akb_link", source=source, target=target, relation="depends_on", edge_type="strong")
    assert promoted["edge_type"] == "strong"
    assert [r["edge_type"] for r in (await mcp("akb_relations", uri=source, type="depends_on"))["relations"]] == ["strong"]


async def test_mcp_refuses_an_invalid_edge_type_and_writes_nothing(world, mcp):
    result = await mcp(
        "akb_link", source=world.uri(A), target=world.uri(B), relation="related_to", edge_type="medium",
    )

    assert result["code"] == "invalid_argument"
    assert await world.edge_rows() == []
