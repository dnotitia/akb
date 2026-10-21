"""Real-PostgreSQL regressions for graph node and relation labels (titles).

The DB-free half is ``test_graph_node_labels_unit.py``. What only a database can
settle is here.

The reported bug: ``/graph`` (neighbourhood and vault overview), ``/graph/overview``,
``akb_graph``, ``/relations`` and ``akb_relations`` named every document by its
collection path and file name even though it had a real title. The lookup behind
them read the legacy ``documents`` catalog alone. A document written by the
native ledger has no row there, so on a ``postgres_native`` installation every
document missed and fell back to its path. The same assertions run against both
document authorities below; the ``native`` run is the one that failed before the
lookup learned to read the head revision's frontmatter.

* a document is named by its title, and also carries ``title`` / ``doc_type`` /
  ``tags`` / ``path``; a document with no title keeps its path as ``name`` and
  states no ``title``; tables and files keep their name;
* the lookup is one batched statement per call — a graph of 25 neighbours costs
  the same as one of 3;
* every resolved resource also carries ``updated_at``, when its own store says it
  last changed, out of those same statements: the head revision's time on a
  native installation (equal to the resource row's and to what the native
  document API serves), ``documents.updated_at`` on a legacy one, a table's and a
  file's own row; a newer write moves it and an unresolved resource states none;
* a native vault is read through the head revision, a document migrated at
  cutover may lean on its retained catalog row for what its body does not say,
  and a pre-cutover document only the catalog holds still resolves;
* one vault's titles never name another vault's documents.

The legacy arm runs on the schema ``init.sql`` builds; the native arm needs the
native ledger, so it gets a fresh database with the migrations real writes meet
(the helper the derived-pipeline tests use). Set ``AKB_TEST_DSN`` to a
PostgreSQL 16 + pgvector instance whose user can create databases;
``REQUIRE_REAL_PG=1`` turns a missing instance from a skip into a failure.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-graph-labels-pg-")

from app.api.deps import get_current_user
from app.api.routes import knowledge
from app.repositories.document_repo import DocumentRepository
from app.services import document_counters, kg_service, native_revision_service
from app.services.document_service import _compose_markdown
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from app.services.uri_service import doc_uri, file_uri, table_uri
from mcp_server import server
from tests.concurrency.test_native_derived_pipeline import _fresh_database

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)

A, B, C = "specs/a.md", "specs/b.md", "notes/c.md"


# ── Moments ──────────────────────────────────────────────────────────────

# Distinct, with microseconds on the first so truncation shows.
T1 = datetime(2026, 10, 20, 13, 8, 25, 987654, tzinfo=UTC)
T2 = datetime(2026, 10, 21, 2, 30, 0, tzinfo=UTC)
T3 = datetime(2026, 10, 22, 9, 0, 59, 500000, tzinfo=UTC)
OLD = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)


def stamp(moment: datetime) -> str:
    """The wire form of a moment, written out here and not by the code under test."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Clock:
    """Stands in for ``datetime`` inside the native service, so each write the
    ledger publishes is stamped with a moment the test chose."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self, _tz=None) -> datetime:
        return self.moment


# ── Worlds: one vault on each document authority ─────────────────────────


class _CountingConnection:
    """Records every statement a call issues, then forwards it unchanged."""

    def __init__(self, conn: asyncpg.Connection, log: list[str]) -> None:
        self._conn = conn
        self._log = log

    def __getattr__(self, name: str):
        attr = getattr(self._conn, name)
        if name not in {"fetch", "fetchrow", "fetchval", "execute"}:
            return attr

        async def _recorded(query: str, *args: Any, **kwargs: Any):
            self._log.append(query)
            return await attr(query, *args, **kwargs)

        return _recorded


class _CountingAcquire:
    def __init__(self, inner, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    async def __aenter__(self):
        return _CountingConnection(await self._inner.__aenter__(), self._log)

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


class _CountingPool:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.statements: list[str] = []

    def acquire(self):
        return _CountingAcquire(self._pool.acquire(), self.statements)

    def __getattr__(self, name: str):
        return getattr(self._pool, name)

    def label_statements(self) -> list[str]:
        """The statements that look a document up by path (the label lookups)."""
        return [s for s in self.statements if "FROM documents" in s or ("native_resources" in s and "unnest" in s)]


@dataclass
class World:
    authority: str  # "legacy" | "native"
    pool: asyncpg.Pool
    vault: str
    vault_id: uuid.UUID
    native: NativeRevisionService | None = None
    resource_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    counting: _CountingPool | None = None
    clock: _Clock | None = None

    def uri(self, path: str) -> str:
        return doc_uri(self.vault, path)

    async def add_doc(
        self, path: str, title: str | None = "", doc_type: str | None = None, tags: list[str] | None = None,
        *, vault_id: uuid.UUID | None = None, at: datetime | None = None, **frontmatter: Any,
    ) -> None:
        """A document with the given metadata on this world's authority.

        ``title=""`` (the default) is a document that has none: the legacy row
        stores an empty title, the native body states no ``title``. ``at`` is the
        moment the store dates the write by: the row's ``updated_at`` on a legacy
        world, the clock the ledger reads on a native one (the ``clock`` fixture).
        """
        vault_id = vault_id or self.vault_id
        if self.authority == "legacy":
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO documents (id, vault_id, path, title, doc_type, tags, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, NOW()))",
                    uuid.uuid4(), vault_id, path, title or "", doc_type, tags, at,
                )
            return
        assert self.native is not None
        if at is not None:
            assert self.clock is not None, "date a native write with the `clock` fixture"
            self.clock.moment = at
        meta: dict[str, Any] = dict(frontmatter)
        if title:
            meta["title"] = title
        if doc_type is not None:
            meta["type"] = doc_type
        if tags is not None:
            meta["tags"] = tags
        created = await self.native.create_text(
            namespace_id=vault_id, surface="document", path=path,
            payload=_compose_markdown(meta, f"body of {path}"), actor="labels-test", mutation_id=uuid.uuid4(),
        )
        self.resource_ids[path] = created.resource_id

    async def link(self, source: str, target: str, relation: str = "related_to", **kwargs: Any) -> dict:
        result = await kg_service.link_resources(
            self.vault, source if source.startswith("akb://") else self.uri(source),
            target if target.startswith("akb://") else self.uri(target), relation, **kwargs,
        )
        assert result.get("linked") is True, result
        return result

    async def graph(self, path: str, **kwargs: Any) -> dict:
        return await kg_service.get_graph(self.vault, self.uri(path), vault_id=self.vault_id, **kwargs)

    async def relations(self, path: str, **kwargs: Any) -> list[dict]:
        return await kg_service.get_resource_relations(
            self.vault, self.uri(path), vault_id=self.vault_id, **kwargs,
        )

    def counted(self) -> _CountingPool:
        assert self.counting is not None
        self.counting.statements.clear()
        return self.counting


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


async def _make_vault(pool: asyncpg.Pool, name: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id", name, f"/tmp/{name}.git",
        )


def _route_through(monkeypatch, pool: asyncpg.Pool) -> _CountingPool:
    counting = _CountingPool(pool)

    async def _get_pool():
        return counting

    monkeypatch.setattr(kg_service, "get_pool", _get_pool)
    return counting


@pytest_asyncio.fixture
async def legacy_pool():
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


@pytest_asyncio.fixture(params=["legacy", "native"])
async def world(request, monkeypatch):
    """One vault on each document authority; the assertions are shared."""
    authority = request.param
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: authority == "native")

    if authority == "legacy":
        if not await _can_connect():
            if os.environ.get("REQUIRE_REAL_PG") == "1":
                pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
            pytest.skip(f"Postgres not reachable at {_DSN}")
        pool = await asyncpg.create_pool(dsn=_DSN, min_size=1, max_size=4)
        async with pool.acquire() as conn:
            await conn.execute((_BACKEND / "app" / "db" / "init.sql").read_text())
        name = f"_test_labels_{uuid.uuid4().hex[:8]}"
        vault_id = await _make_vault(pool, name)
        built = World("legacy", pool, name, vault_id)
        built.counting = _route_through(monkeypatch, pool)
        try:
            yield built
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)
            await pool.close()
        return

    async with _fresh_database() as pool:
        name = f"labels-{uuid.uuid4().hex[:8]}"
        vault_id = await _make_vault(pool, name)
        built = World("native", pool, name, vault_id, native=NativeRevisionService(pool, payload_store=M1PgBodyStore(pool)))
        built.counting = _route_through(monkeypatch, pool)
        yield built


# ── The reported bug, on each authority ──────────────────────────────────


async def test_the_neighbourhood_names_documents_by_their_title(world):
    await world.add_doc(A, "Alpha design", "spec", ["graph", "design"])
    await world.add_doc(B, "Beta notes", "note", ["notes"])
    await world.link(A, B)

    graph = await world.graph(A, hops=1)

    by_uri = {n["uri"]: n for n in graph["nodes"]}
    assert by_uri[world.uri(A)]["name"] == "Alpha design"  # was "specs/a.md"
    assert by_uri[world.uri(B)]["name"] == "Beta notes"  # was "specs/b.md"
    assert {k: by_uri[world.uri(A)][k] for k in ("title", "doc_type", "tags", "path", "depth")} == {
        "title": "Alpha design", "doc_type": "spec", "tags": ["graph", "design"], "path": A, "depth": 0,
    }
    assert {k: by_uri[world.uri(B)][k] for k in ("title", "doc_type", "tags", "path", "depth")} == {
        "title": "Beta notes", "doc_type": "note", "tags": ["notes"], "path": B, "depth": 1,
    }


async def test_the_vault_overview_names_documents_by_their_title(world):
    await world.add_doc(A, "Alpha design", "spec", ["graph"])
    await world.add_doc(B, "Beta notes")
    await world.link(A, B)

    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id, include_orphans=False)
    whole_vault = await kg_service.get_graph(world.vault, None, vault_id=world.vault_id)

    for payload in (overview, whole_vault):
        by_uri = {n["uri"]: n for n in payload["nodes"]}
        assert by_uri[world.uri(A)]["name"] == "Alpha design"
        assert by_uri[world.uri(B)]["name"] == "Beta notes"
        assert (by_uri[world.uri(A)]["doc_type"], by_uri[world.uri(A)]["tags"]) == ("spec", ["graph"])
        assert by_uri[world.uri(A)]["degree"] == 1  # existing fields are untouched
    assert overview["nodes_total"] == 2 and overview["truncated"] is False


async def test_a_relation_row_names_the_other_end_by_its_title(world):
    await world.add_doc(A, "Alpha design", "spec")
    await world.add_doc(B, "Beta notes", "note", ["n"])
    await world.link(A, B)

    (outgoing,) = await world.relations(A)
    (incoming,) = await world.relations(B)

    assert outgoing["uri"] == world.uri(B)
    assert (outgoing["name"], outgoing["title"], outgoing["doc_type"], outgoing["tags"], outgoing["path"]) == (
        "Beta notes", "Beta notes", "note", ["n"], B,
    )
    assert incoming["uri"] == world.uri(A)
    assert (incoming["name"], incoming["title"], incoming["doc_type"]) == ("Alpha design", "Alpha design", "spec")
    # The row's own fields are untouched.
    assert (outgoing["direction"], outgoing["relation"], outgoing["kind"], outgoing["edge_type"]) == (
        "outgoing", "related_to", "explicit", "strong",
    )


async def test_health_hubs_are_named_by_their_title(world):
    await world.add_doc(A, "Alpha design", "spec")
    for i in range(3):
        await world.add_doc(f"leaf/l{i}.md", f"Leaf {i}")
        await world.link(A, f"leaf/l{i}.md")

    health = await kg_service.get_health(world.vault, vault_id=world.vault_id, hub_threshold=3)

    (hub,) = health["hubs"]
    assert (hub["uri"], hub["name"], hub["degree"], hub["title"], hub["doc_type"]) == (
        world.uri(A), "Alpha design", 3, "Alpha design", "spec",
    )


async def test_a_document_with_no_title_keeps_its_path_as_its_name(world):
    await world.add_doc(A, "Alpha design")
    await world.add_doc(B, title="")  # no title anywhere
    await world.link(A, B)

    graph = await world.graph(A, hops=1)
    (row,) = await world.relations(A)

    untitled = next(n for n in graph["nodes"] if n["uri"] == world.uri(B))
    assert untitled["name"] == B  # today's value, unchanged
    assert "title" not in untitled
    assert (untitled["doc_type"], untitled["tags"], untitled["path"]) == ("note", [], B)
    assert row["name"] == B and "title" not in row


async def test_a_dangling_endpoint_is_named_by_its_path_and_does_not_break_the_graph(world):
    """An edge whose document went away is still served, named by its path."""
    await world.add_doc(A, "Alpha design")
    await world.add_doc(B, "Beta notes")
    await world.link(A, B)
    # The document disappears behind the edge (nothing has cleaned the edge up yet).
    if world.authority == "legacy":
        async with world.pool.acquire() as conn:
            await conn.execute("DELETE FROM documents WHERE vault_id = $1 AND path = $2", world.vault_id, B)
    else:
        assert world.native is not None
        await world.native.delete_resource(
            namespace_id=world.vault_id, surface="document", path=B, actor="labels-test",
            mutation_id=uuid.uuid4(), expected_revision_id=await _head(world, B),
        )

    graph = await world.graph(A, hops=1)

    ghost = next(n for n in graph["nodes"] if n["uri"] == world.uri(B))
    assert ghost["name"] == B
    assert set(ghost) == {"uri", "resource_type", "name", "depth", "path"}  # nothing claims it was found


async def test_tables_and_files_keep_their_name(world):
    await world.add_doc(A, "Alpha design")
    file_id = uuid.uuid4()
    async with world.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_tables (vault_id, name, description) VALUES ($1, 'metrics', 'Key metrics')", world.vault_id,
        )
        await conn.execute(
            "INSERT INTO vault_tables (vault_id, name) VALUES ($1, 'plain')", world.vault_id,
        )
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, kind, upload_state, name, s3_key) "
            "VALUES ($1, $2, 'file', 'confirmed', 'diagram.png', $3)",
            file_id, world.vault_id, f"k/{file_id}",
        )
    table_with_description, plain_table, file = table_uri(world.vault, "metrics"), table_uri(world.vault, "plain"), file_uri(world.vault, str(file_id))
    for target, relation in ((table_with_description, "references"), (plain_table, "depends_on"), (file, "attached_to")):
        await world.link(world.uri(A), target, relation)

    graph = await world.graph(A, hops=1)

    by_uri = {n["uri"]: n for n in graph["nodes"]}
    assert (by_uri[table_with_description]["name"], by_uri[table_with_description]["title"]) == ("Key metrics", "Key metrics")
    assert (by_uri[plain_table]["name"], by_uri[plain_table]["title"]) == ("plain", "plain")
    assert (by_uri[file]["name"], by_uri[file]["title"]) == ("diagram.png", "diagram.png")
    assert all("doc_type" not in by_uri[u] for u in (table_with_description, plain_table, file))


async def test_labels_never_cross_vaults(world):
    """The same path in two vaults is two documents; neither names the other."""
    other = f"{world.vault}-other"
    other_id = await _make_vault(world.pool, other)
    try:
        await world.add_doc(A, "Alpha in the first vault")
        await world.add_doc(B, "Beta in the first vault")
        await world.add_doc(A, "Alpha in the OTHER vault", vault_id=other_id)
        await world.add_doc(B, "Beta in the OTHER vault", vault_id=other_id)
        await world.link(A, B)

        graph = await world.graph(A, hops=1)
        (row,) = await world.relations(A)

        assert {n["name"] for n in graph["nodes"]} == {"Alpha in the first vault", "Beta in the first vault"}
        assert row["name"] == "Beta in the first vault"
    finally:
        async with world.pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", other_id)


# ── The statement budget ─────────────────────────────────────────────────


async def _star(world: World, hub: str, leaves: int) -> None:
    await world.add_doc(hub, f"Hub {hub}")
    for i in range(leaves):
        leaf = f"{hub.removesuffix('.md')}/leaf-{i:02d}.md"
        await world.add_doc(leaf, f"Leaf {i} of {hub}")
        await world.link(hub, leaf)


async def test_a_graph_costs_one_label_statement_and_the_same_total_however_many_nodes(world):
    """Not one lookup per node: 25 neighbours cost exactly what 3 do."""
    await _star(world, "small.md", 3)
    await _star(world, "big.md", 25)

    walked: dict[str, tuple[int, int]] = {}
    for hub, leaves in (("small.md", 3), ("big.md", 25)):
        counting = world.counted()
        graph = await world.graph(hub, hops=1, limit=50)
        assert len(graph["nodes"]) == leaves + 1
        assert {n["name"] for n in graph["nodes"]} == {f"Hub {hub}", *(f"Leaf {i} of {hub}" for i in range(leaves))}
        # The time of every node came out of that same statement.
        assert all(isinstance(n.get("updated_at"), str) for n in graph["nodes"])
        walked[hub] = (len(counting.statements), len(counting.label_statements()))

    assert walked["small.md"] == walked["big.md"], walked
    assert walked["big.md"][1] == 1  # one statement names every node


async def test_relations_and_the_overview_cost_one_label_statement_too(world):
    await _star(world, "small.md", 3)
    await _star(world, "big.md", 25)

    relation_statements = []
    for hub, leaves in (("small.md", 3), ("big.md", 25)):
        counting = world.counted()
        rows = await world.relations(hub)
        assert len(rows) == leaves
        assert len(counting.label_statements()) == 1
        relation_statements.append(len(counting.statements))
    assert relation_statements[0] == relation_statements[1]

    counting = world.counted()
    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id, include_orphans=False)
    assert len(overview["nodes"]) == 2 + 3 + 25
    assert len(counting.label_statements()) == 1


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


@pytest_asyncio.fixture
async def mcp(world, monkeypatch):
    async def _access(*_args, **_kwargs):
        return {"vault_id": world.vault_id, "role": "reader"}

    monkeypatch.setattr(server, "check_vault_access", _access)
    user = server._MCPUser(user_id=str(uuid.uuid4()), username="tester")

    async def call(tool: str, **arguments: Any) -> dict:
        return await server._dispatch(tool, arguments, user)

    return call


async def test_rest_serves_titles_on_every_graph_and_relations_route(world, rest):
    await world.add_doc(A, "Alpha design", "spec", ["graph"])
    await world.add_doc(B, "Beta notes", "note")
    await world.link(A, B)

    neighbours = (await rest.get("/api/v1/graph", params={"uri": world.uri(A), "hops": 1})).json()
    overview = (await rest.get("/api/v1/graph/overview", params={"vault": world.vault})).json()
    whole_vault = (await rest.get("/api/v1/graph", params={"vault": world.vault})).json()
    relations = (await rest.get("/api/v1/relations", params={"uri": world.uri(A)})).json()

    assert neighbours["kind"] == "graph_neighbors"
    assert overview["kind"] == whole_vault["kind"] == "graph_overview"
    for payload in (neighbours, overview, whole_vault):
        by_uri = {n["uri"]: n for n in payload["nodes"]}
        assert by_uri[world.uri(A)]["name"] == "Alpha design"
        assert by_uri[world.uri(A)]["title"] == "Alpha design"
        assert by_uri[world.uri(A)]["doc_type"] == "spec"
        assert by_uri[world.uri(A)]["tags"] == ["graph"]
        assert by_uri[world.uri(A)]["path"] == A
        assert by_uri[world.uri(B)]["name"] == "Beta notes"
    (row,) = relations["relations"]
    assert (row["name"], row["title"], row["doc_type"], row["path"]) == ("Beta notes", "Beta notes", "note", B)


async def test_mcp_serves_titles_on_akb_graph_and_akb_relations(world, mcp):
    await world.add_doc(A, "Alpha design", "spec")
    await world.add_doc(B, "Beta notes")
    await world.link(A, B)

    neighbours = await mcp("akb_graph", uri=world.uri(A), hops=1)
    whole_vault = await mcp("akb_graph", vault=world.vault)
    relations = await mcp("akb_relations", uri=world.uri(A))

    for payload in (neighbours, whole_vault):
        assert {n["name"] for n in payload["nodes"]} == {"Alpha design", "Beta notes"}
    (row,) = relations["relations"]
    assert (row["name"], row["title"]) == ("Beta notes", "Beta notes")


# ── Legacy-only surfaces ─────────────────────────────────────────────────


@pytest_asyncio.fixture
async def legacy_world(monkeypatch, legacy_pool):
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: False)
    name = f"_test_labels_legacy_{uuid.uuid4().hex[:8]}"
    vault_id = await _make_vault(legacy_pool, name)
    built = World("legacy", legacy_pool, name, vault_id)
    built.counting = _route_through(monkeypatch, legacy_pool)
    try:
        yield built
    finally:
        async with legacy_pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)


async def test_provenance_relations_are_titled(legacy_world):
    world = legacy_world
    await world.add_doc(A, "Alpha design")
    await world.add_doc(B, "Beta notes", "note", ["n"])
    await world.link(A, B)
    async with world.pool.acquire() as conn:
        doc_id = await conn.fetchval("SELECT id FROM documents WHERE vault_id = $1 AND path = $2", world.vault_id, A)

    provenance = await kg_service.get_provenance(str(doc_id), vault_id=world.vault_id)

    (row,) = provenance["relations"]
    assert (row["name"], row["title"], row["doc_type"], row["tags"]) == ("Beta notes", "Beta notes", "note", ["n"])


async def test_orphan_documents_in_the_overview_are_labelled_like_connected_ones(legacy_world):
    world = legacy_world
    await world.add_doc(A, "Alpha design")
    await world.add_doc(B, "Beta notes")
    await world.link(A, B)
    await world.add_doc(C, "Lonely note", "reference", ["solo"])

    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id)
    async with world.pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT updated_at FROM documents WHERE vault_id = $1 AND path = $2", world.vault_id, C,
        )

    lonely = next(n for n in overview["nodes"] if n["uri"] == world.uri(C))
    assert lonely == {
        "uri": world.uri(C), "resource_type": "doc", "degree": 0, "name": "Lonely note",
        "title": "Lonely note", "doc_type": "reference", "tags": ["solo"], "path": C,
        "updated_at": stamp(stored),  # its catalog row's time, like a connected document's
    }
    assert overview["orphans_returned"] == 1


# ── Native authority: what the head revision and the catalog each contribute ──


@pytest_asyncio.fixture
async def native_world(monkeypatch):
    monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: True)
    async with _fresh_database() as pool:
        name = f"labels-{uuid.uuid4().hex[:8]}"
        vault_id = await _make_vault(pool, name)
        built = World("native", pool, name, vault_id, native=NativeRevisionService(pool, payload_store=M1PgBodyStore(pool)))
        built.counting = _route_through(monkeypatch, pool)
        yield built


def _pin_clock(world: World, monkeypatch) -> _Clock:
    """Make every native write on ``world`` land at ``clock.moment``."""
    clock = _Clock(T1)
    monkeypatch.setattr(native_revision_service, "datetime", clock)
    world.clock = clock
    return clock


@pytest.fixture
def clock(world, monkeypatch) -> _Clock:
    """The ledger's clock on the ``world`` fixture (inert on a legacy world, which
    dates a write by the row it inserts)."""
    return _pin_clock(world, monkeypatch)


@pytest.fixture
def native_clock(native_world, monkeypatch) -> _Clock:
    return _pin_clock(native_world, monkeypatch)


async def test_a_native_vault_has_no_catalog_rows_so_the_title_can_only_come_from_the_body(native_world):
    """The precondition that made the lookup miss: nothing in `documents`."""
    world = native_world
    await world.add_doc(A, "Alpha design", "spec", ["graph"])
    await world.add_doc(B, "Beta notes")
    await world.link(A, B)

    async with world.pool.acquire() as conn:
        catalog_rows = await conn.fetchval("SELECT count(*) FROM documents WHERE vault_id = $1", world.vault_id)
        native_rows = await conn.fetchval(
            "SELECT count(*) FROM native_resources WHERE namespace_id = $1 AND surface = 'document'", world.vault_id,
        )
    graph = await world.graph(A, hops=1)

    assert (catalog_rows, native_rows) == (0, 2)
    assert {n["name"] for n in graph["nodes"]} == {"Alpha design", "Beta notes"}


async def test_the_title_follows_an_edit_of_the_head_revision(native_world):
    world = native_world
    await world.add_doc(A, "First title")
    await world.add_doc(B, "Beta notes")
    await world.link(A, B)
    await world.native.replace_text(
        namespace_id=world.vault_id, surface="document", path=A,
        payload=_compose_markdown({"title": "Second title", "type": "spec"}, "edited"),
        actor="labels-test", mutation_id=uuid.uuid4(),
        expected_revision_id=(await _head(world, A)), expected_resource_id=world.resource_ids[A],
    )

    graph = await world.graph(A, hops=1)

    node = next(n for n in graph["nodes"] if n["uri"] == world.uri(A))
    assert (node["name"], node["doc_type"]) == ("Second title", "spec")


async def _head(world: World, path: str) -> str:
    async with world.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT head_revision_id FROM native_resources WHERE namespace_id = $1 AND current_path = $2 AND lifecycle = 'live'",
            world.vault_id, path,
        )


async def _retain_catalog_row(
    world: World, path: str, title: str, doc_type: str | None, tags: list[str], *, resource_id,
    updated_at: datetime | None = None,
) -> None:
    async with world.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, tags, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, NOW()))",
            resource_id, world.vault_id, path, title, doc_type, tags, updated_at,
        )


async def test_a_retained_catalog_row_fills_what_the_preserved_body_does_not_say(native_world):
    """A document migrated at cutover: its body is byte-for-byte the Git file and
    may state no title; the catalog row it kept still does."""
    world = native_world
    await world.add_doc(A, title="", doc_type="decision")  # the body states a type, no title, no tags
    await world.add_doc(B, "Beta notes", "spec", ["fresh"])  # the body states everything
    await world.link(A, B)
    await _retain_catalog_row(world, A, "Catalog title", "note", ["from-catalog"], resource_id=world.resource_ids[A])
    await _retain_catalog_row(world, B, "Stale catalog title", "note", ["stale"], resource_id=world.resource_ids[B])

    graph = await world.graph(A, hops=1)

    by_uri = {n["uri"]: n for n in graph["nodes"]}
    a, b = by_uri[world.uri(A)], by_uri[world.uri(B)]
    assert (a["name"], a["doc_type"], a["tags"]) == ("Catalog title", "decision", ["from-catalog"])
    assert (b["name"], b["doc_type"], b["tags"]) == ("Beta notes", "spec", ["fresh"])  # the body wins


async def test_a_pre_cutover_document_only_the_catalog_holds_still_resolves(native_world):
    world = native_world
    await world.add_doc(A, "Alpha design")
    # `notes/c.md` was never copied into the ledger: the catalog is all there is.
    async with world.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, tags) VALUES ($1, $2, $3, 'Old note', 'note', '{t}')",
            uuid.uuid4(), world.vault_id, C,
        )
        await conn.execute(
            "INSERT INTO edges (vault_id, source_uri, target_uri, relation_type, source_type, target_type, kind) "
            "VALUES ($1, $2, $3, 'related_to', 'doc', 'doc', 'explicit')",
            world.vault_id, world.uri(A), world.uri(C),
        )

    graph = await world.graph(A, hops=1)

    old = next(n for n in graph["nodes"] if n["uri"] == world.uri(C))
    assert (old["name"], old["doc_type"], old["tags"]) == ("Old note", "note", ["t"])


async def test_a_catalog_row_at_a_path_a_native_resource_holds_does_not_name_it(native_world):
    """The catalog is consulted by identity once the ledger holds the path; a
    ghost row that merely shares the path must not lend its title."""
    world = native_world
    await world.add_doc(A, title="")  # native, no title in the body
    await world.add_doc(B, "Beta notes")
    await world.link(A, B)
    await _retain_catalog_row(world, A, "Ghost of another document", "note", [], resource_id=uuid.uuid4())

    graph = await world.graph(A, hops=1)

    node = next(n for n in graph["nodes"] if n["uri"] == world.uri(A))
    assert node["name"] == A and "title" not in node


async def test_a_hub_with_a_frontmatter_longer_than_the_first_read_is_still_titled(native_world):
    """Frontmatter keys sort alphabetically, so `title` follows a long `related_to`."""
    world = native_world
    children = [f"akb://{world.vault}/coll/hub/doc/child-{i:04d}.md" for i in range(300)]
    await world.add_doc("hub/index.md", "Hub index", "index", related_to=children)
    await world.add_doc(B, "Beta notes")
    await world.link("hub/index.md", B)
    async with world.pool.acquire() as conn:
        size = await conn.fetchval(
            "SELECT p.byte_size FROM native_resources r "
            "JOIN native_revisions nr ON nr.resource_id = r.resource_id AND nr.revision_id = r.head_revision_id "
            "JOIN native_payload_manifests pm ON pm.payload_manifest_id = nr.payload_manifest_id "
            "JOIN m1_reference_payloads p ON p.payload_id = pm.private_locator "
            "WHERE r.resource_id = $1",
            world.resource_ids["hub/index.md"],
        )
    assert size > 8 * 1024 + 1, "the fixture must outgrow the first read window"

    counting = world.counted()
    graph = await world.graph("hub/index.md", hops=1)

    hub = next(n for n in graph["nodes"] if n["uri"] == world.uri("hub/index.md"))
    assert (hub["name"], hub["doc_type"]) == ("Hub index", "index")
    # One wider read, for that document only.
    assert len(counting.label_statements()) == 2


async def test_a_document_without_frontmatter_is_named_by_its_path(native_world):
    world = native_world
    await world.add_doc(A, "Alpha design")
    created = await world.native.create_text(
        namespace_id=world.vault_id, surface="document", path=B,
        payload="# just a heading\n\nand prose\n", actor="labels-test", mutation_id=uuid.uuid4(),
    )
    assert created.resource_id
    await world.link(A, B)

    graph = await world.graph(A, hops=1)

    plain = next(n for n in graph["nodes"] if n["uri"] == world.uri(B))
    assert (plain["name"], plain["doc_type"], plain["tags"]) == (B, "note", [])


async def test_a_legacy_installation_does_not_read_the_native_ledger(monkeypatch, legacy_world):
    """Switching the authority off is what keeps native tables out of the query."""
    world = legacy_world
    await world.add_doc(A, "Alpha design")
    await world.add_doc(B, "Beta notes")
    await world.link(A, B)

    counting = world.counted()
    graph = await world.graph(A, hops=1)

    assert {n["name"] for n in graph["nodes"]} == {"Alpha design", "Beta notes"}
    assert not any("native_" in statement for statement in counting.statements)


# ── When the resource last changed ───────────────────────────────────────
#
# Every resolved resource carries `updated_at`, out of the statements that name
# it. The store it comes from depends on the resource and on the installation's
# document authority; what each means is pinned below against real rows.


async def _stored_time(world: World, path: str) -> datetime:
    """When this world's own store says the document at ``path`` last changed."""
    async with world.pool.acquire() as conn:
        if world.authority == "legacy":
            return await conn.fetchval(
                "SELECT updated_at FROM documents WHERE vault_id = $1 AND path = $2", world.vault_id, path,
            )
        return await conn.fetchval(
            "SELECT updated_at FROM native_resources "
            "WHERE namespace_id = $1 AND current_path = $2 AND lifecycle = 'live'",
            world.vault_id, path,
        )


async def _write_newer(world: World, path: str, at: datetime, title: str = "Edited") -> None:
    """A later edit of an existing document, made the way this authority makes one."""
    if world.authority == "legacy":
        async with world.pool.acquire() as conn:
            doc_id = await conn.fetchval(
                "SELECT id FROM documents WHERE vault_id = $1 AND path = $2", world.vault_id, path,
            )
        await DocumentRepository(world.pool).update(doc_id, title=title, now=at)
        return
    assert world.native is not None and world.clock is not None
    world.clock.moment = at
    await world.native.replace_text(
        namespace_id=world.vault_id, surface="document", path=path,
        payload=_compose_markdown({"title": title}, "edited"), actor="labels-test",
        mutation_id=uuid.uuid4(), expected_revision_id=await _head(world, path),
        expected_resource_id=world.resource_ids[path],
    )


async def _add_table_and_file(world: World, *, table_at: datetime, file_at: datetime) -> tuple[str, str]:
    """A described table and a confirmed file with the given row times; their URIs."""
    file_id = uuid.uuid4()
    async with world.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vault_tables (vault_id, name, description, updated_at) "
            "VALUES ($1, 'metrics', 'Key metrics', $2)",
            world.vault_id, table_at,
        )
        await conn.execute(
            "INSERT INTO vault_files (id, vault_id, kind, upload_state, name, s3_key, updated_at) "
            "VALUES ($1, $2, 'file', 'confirmed', 'diagram.png', $3, $4)",
            file_id, world.vault_id, f"k/{file_id}", file_at,
        )
    return table_uri(world.vault, "metrics"), file_uri(world.vault, str(file_id))


def _times(nodes: list[dict]) -> dict[str, str | None]:
    return {n["uri"]: n.get("updated_at") for n in nodes}


async def test_every_graph_surface_carries_the_time_its_own_store_holds(world, clock):
    await world.add_doc(A, "Alpha design", "spec", at=T1)
    await world.add_doc(B, "Beta notes", at=T2)
    table, file = await _add_table_and_file(world, table_at=T3, file_at=OLD)
    await world.link(A, B)
    await world.link(world.uri(A), table, "references")
    await world.link(world.uri(A), file, "attached_to")
    want = {
        world.uri(A): stamp(T1), world.uri(B): stamp(T2), table: stamp(T3), file: stamp(OLD),
    }
    assert stamp(T1) == "2026-10-20T13:08:25Z"  # microseconds truncated, not rounded

    neighbourhood = await world.graph(A, hops=1)
    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id, include_orphans=False)
    whole_vault = await kg_service.get_graph(world.vault, None, vault_id=world.vault_id)
    rows = await world.relations(A)
    health = await kg_service.get_health(world.vault, vault_id=world.vault_id, hub_threshold=3)

    assert _times(neighbourhood["nodes"]) == want
    assert _times(overview["nodes"]) == want
    assert _times(whole_vault["nodes"]) == want
    assert {row["uri"]: row["updated_at"] for row in rows} == {k: v for k, v in want.items() if k != world.uri(A)}
    (hub,) = health["hubs"]
    assert (hub["uri"], hub["updated_at"]) == (world.uri(A), stamp(T1))
    # The store really holds what the label says (no second source in play).
    assert stamp(await _stored_time(world, A)) == stamp(T1)
    assert stamp(await _stored_time(world, B)) == stamp(T2)


async def test_a_newer_write_moves_the_time_and_leaves_the_neighbours_alone(world, clock):
    await world.add_doc(A, "First title", at=T1)
    await world.add_doc(B, "Beta notes", at=T1)
    await world.link(A, B)
    first = _times((await world.graph(A, hops=1))["nodes"])
    assert first == {world.uri(A): stamp(T1), world.uri(B): stamp(T1)}

    await _write_newer(world, A, T3, title="Second title")

    second = await world.graph(A, hops=1)
    assert _times(second["nodes"]) == {world.uri(A): stamp(T3), world.uri(B): stamp(T1)}
    assert next(n for n in second["nodes"] if n["uri"] == world.uri(A))["name"] == "Second title"
    (seen_from_b,) = await world.relations(B)
    assert (seen_from_b["uri"], seen_from_b["updated_at"]) == (world.uri(A), stamp(T3))
    assert stamp(await _stored_time(world, A)) == stamp(T3)


async def test_a_move_counts_as_a_change_on_both_authorities(world, clock):
    """`documents.updated_at` moves with the path, and so does the resource row."""
    new_path = "moved/a.md"
    await world.add_doc(A, "Alpha design", at=T1)
    if world.authority == "legacy":
        async with world.pool.acquire() as conn:
            doc_id = await conn.fetchval(
                "SELECT id FROM documents WHERE vault_id = $1 AND path = $2", world.vault_id, A,
            )
            await DocumentRepository(world.pool).update_path(
                doc_id, new_path, collection_id=None, now=T2, commit_hash="0" * 40, conn=conn,
            )
    else:
        assert world.native is not None
        clock.moment = T2
        await world.native.move_text(
            namespace_id=world.vault_id, surface="document", path=A, path_to=new_path,
            actor="labels-test", mutation_id=uuid.uuid4(), expected_revision_id=await _head(world, A),
            expected_resource_id=world.resource_ids[A],
        )

    graph = await world.graph(new_path, hops=1)

    (node,) = graph["nodes"]
    assert (node["path"], node["updated_at"]) == (new_path, stamp(T2))


# ── REST and MCP: the time is served, and absent where it is unknown ─────


async def _delete_behind_the_edge(world: World, path: str) -> None:
    """The document disappears, its edge stays (nothing has cleaned it up yet)."""
    if world.authority == "legacy":
        async with world.pool.acquire() as conn:
            await conn.execute("DELETE FROM documents WHERE vault_id = $1 AND path = $2", world.vault_id, path)
        return
    assert world.native is not None
    await world.native.delete_resource(
        namespace_id=world.vault_id, surface="document", path=path, actor="labels-test",
        mutation_id=uuid.uuid4(), expected_revision_id=await _head(world, path),
    )


async def test_rest_and_mcp_serve_the_time_and_leave_it_out_when_unknown(world, clock, rest, mcp):
    await world.add_doc(A, "Alpha design", "spec", at=T1)
    await world.add_doc(B, "Beta notes", at=T2)
    await world.add_doc(C, "Gone soon", at=T3)
    await world.link(A, B)
    await world.link(A, C)
    await _delete_behind_the_edge(world, C)
    ghost = world.uri(C)
    want = {world.uri(A): stamp(T1), world.uri(B): stamp(T2), ghost: None}

    neighbours = (await rest.get("/api/v1/graph", params={"uri": world.uri(A), "hops": 1})).json()
    whole_vault = (await rest.get("/api/v1/graph", params={"vault": world.vault})).json()
    overview = (await rest.get("/api/v1/graph/overview", params={"vault": world.vault})).json()
    relations = (await rest.get("/api/v1/relations", params={"uri": world.uri(A)})).json()
    health = (await rest.get("/api/v1/graph/health", params={"vault": world.vault, "hub_threshold": 2})).json()
    mcp_neighbours = await mcp("akb_graph", uri=world.uri(A), hops=1)
    mcp_whole_vault = await mcp("akb_graph", vault=world.vault)
    mcp_relations = await mcp("akb_relations", uri=world.uri(A))

    for payload in (neighbours, whole_vault, overview, mcp_neighbours, mcp_whole_vault):
        assert _times(payload["nodes"]) == want
        # Left out, not null: a client that tests for the key is not misled.
        assert "updated_at" not in next(n for n in payload["nodes"] if n["uri"] == ghost)
    for payload in (relations, mcp_relations):
        rows = {row["uri"]: row for row in payload["relations"]}
        assert rows[world.uri(B)]["updated_at"] == stamp(T2)
        assert "updated_at" not in rows[ghost]
    (hub,) = health["hubs"]
    assert (hub["uri"], hub["updated_at"]) == (world.uri(A), stamp(T1))


async def test_a_row_time_of_infinity_names_no_moment(legacy_world):
    """PostgreSQL can hold `infinity`; asyncpg hands it back as the far end of the
    calendar. Serving that as a date would invent a change in the year 9999."""
    world = legacy_world
    await world.add_doc(A, "Alpha design", at=T1)
    await world.add_doc(B, "Beta notes")
    async with world.pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET updated_at = 'infinity' WHERE vault_id = $1 AND path = $2", world.vault_id, B,
        )
    await world.link(A, B)

    graph = await world.graph(A, hops=1)

    by_uri = {n["uri"]: n for n in graph["nodes"]}
    assert by_uri[world.uri(A)]["updated_at"] == stamp(T1)
    assert "updated_at" not in by_uri[world.uri(B)]
    assert by_uri[world.uri(B)]["name"] == "Beta notes"  # everything else about it is served


# ── Table and file: their own rows, authority-independent ────────────────


async def test_a_table_and_a_file_are_dated_by_their_own_rows_not_the_ledger(world):
    """Tables are not documents and the File catalogue is the authority for files
    on either installation, so neither is dated through the document lookup."""
    await world.add_doc(A, "Alpha design")
    table, file = await _add_table_and_file(world, table_at=T3, file_at=T2)
    await world.link(world.uri(A), table, "references")
    await world.link(world.uri(A), file, "attached_to")

    graph = await world.graph(A, hops=1)

    by_uri = {n["uri"]: n for n in graph["nodes"]}
    assert by_uri[table]["updated_at"] == stamp(T3)
    assert by_uri[file]["updated_at"] == stamp(T2)
    assert (by_uri[table]["name"], by_uri[file]["name"]) == ("Key metrics", "diagram.png")  # labels unchanged


# ── Orphans in the overview ──────────────────────────────────────────────


async def test_orphan_tables_and_files_carry_their_own_times_too(world):
    table, file = await _add_table_and_file(world, table_at=T3, file_at=T2)

    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id)

    by_uri = {n["uri"]: n for n in overview["nodes"]}
    assert by_uri[table]["updated_at"] == stamp(T3)
    assert by_uri[file]["updated_at"] == stamp(T2)
    assert overview["orphans_returned"] >= 2


async def test_a_legacy_overview_with_orphans_never_reads_the_native_ledger(legacy_world):
    world = legacy_world
    await world.add_doc(A, "Alpha design")
    await world.add_doc(C, "Lonely note")  # an orphan
    await _add_table_and_file(world, table_at=T3, file_at=T2)

    counting = world.counted()
    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id)

    assert {n["uri"] for n in overview["nodes"]} >= {world.uri(A), world.uri(C)}
    assert all("updated_at" in n for n in overview["nodes"])
    assert not any("native_" in statement for statement in counting.statements)


# ── Native authority: where the time comes from ──────────────────────────


async def test_the_native_time_is_one_instant_for_the_label_the_head_revision_and_the_document_api(
    native_world, native_clock,
):
    """One value, four readers. The label reads the resource row; the head
    revision's own `occurred_at` and the time the native document API serves as
    `updated_at` are the same instant (exact, not to the second), after a create,
    an edit and a move alike."""
    world = native_world
    assert world.native is not None

    async def agree(path: str, at: datetime, action: str) -> None:
        async with world.pool.acquire() as conn:
            head = await conn.fetchrow(
                "SELECT r.updated_at AS resource_time, nr.occurred_at AS head_time, nr.action "
                "FROM native_resources r JOIN native_revisions nr "
                "  ON nr.resource_id = r.resource_id AND nr.revision_id = r.head_revision_id "
                "WHERE r.namespace_id = $1 AND r.current_path = $2 AND r.lifecycle = 'live'",
                world.vault_id, path,
            )
        snapshot = await world.native.get_current_reference(
            namespace_id=world.vault_id, surface="document", reference=path,
        )
        (node,) = (await world.graph(path, hops=1))["nodes"]
        assert head["action"] == action
        assert head["resource_time"] == head["head_time"] == snapshot.resource_updated_at == at
        assert node["updated_at"] == stamp(at)

    await world.add_doc(A, "Alpha design", at=T1)
    await agree(A, T1, "create")

    native_clock.moment = T2
    await world.native.replace_text(
        namespace_id=world.vault_id, surface="document", path=A,
        payload=_compose_markdown({"title": "Alpha design v2"}, "edited"), actor="labels-test",
        mutation_id=uuid.uuid4(), expected_revision_id=await _head(world, A),
        expected_resource_id=world.resource_ids[A],
    )
    await agree(A, T2, "replace")

    native_clock.moment = T3
    await world.native.move_text(
        namespace_id=world.vault_id, surface="document", path=A, path_to="moved/a.md",
        actor="labels-test", mutation_id=uuid.uuid4(), expected_revision_id=await _head(world, A),
        expected_resource_id=world.resource_ids[A],
    )
    await agree("moved/a.md", T3, "move")


async def test_a_body_that_states_its_own_updated_at_does_not_date_the_node(native_world, native_clock):
    """A body kept byte for byte from Git states its own `updated_at`; so does any
    frontmatter an author writes. The moment the head was published is what counts."""
    world = native_world
    await world.add_doc(
        A, "Alpha design", at=T2, updated_at="1999-01-01T00:00:00Z", created_at="1998-01-01T00:00:00Z",
    )

    node = next(n for n in (await world.graph(A, hops=1))["nodes"] if n["uri"] == world.uri(A))

    assert node["updated_at"] == stamp(T2)


async def test_a_retained_catalog_row_never_dates_a_native_document(native_world, native_clock):
    """Once the ledger holds a document, the row it kept at cutover (frozen) dates
    nothing, whether that row says earlier or later than the resource."""
    world = native_world
    await world.add_doc(A, "Alpha design", at=T2)
    await world.add_doc(B, "Beta notes", at=T2)
    await world.link(A, B)
    await _retain_catalog_row(world, A, "Frozen A", "note", [], resource_id=world.resource_ids[A], updated_at=OLD)
    future = datetime(2030, 1, 1, tzinfo=UTC)
    await _retain_catalog_row(world, B, "Frozen B", "note", [], resource_id=world.resource_ids[B], updated_at=future)

    graph = await world.graph(A, hops=1)

    assert _times(graph["nodes"]) == {world.uri(A): stamp(T2), world.uri(B): stamp(T2)}


async def test_a_pre_cutover_document_only_the_catalog_holds_is_dated_by_its_row(native_world, native_clock):
    world = native_world
    await world.add_doc(A, "Alpha design", at=T2)
    async with world.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, tags, updated_at) "
            "VALUES ($1, $2, $3, 'Old note', 'note', '{t}', $4)",
            uuid.uuid4(), world.vault_id, C, OLD,
        )
        await conn.execute(
            "INSERT INTO edges (vault_id, source_uri, target_uri, relation_type, source_type, target_type, kind) "
            "VALUES ($1, $2, $3, 'related_to', 'doc', 'doc', 'explicit')",
            world.vault_id, world.uri(A), world.uri(C),
        )

    graph = await world.graph(A, hops=1)

    assert _times(graph["nodes"]) == {world.uri(A): stamp(T2), world.uri(C): stamp(OLD)}


async def test_a_wide_frontmatter_hub_is_dated_by_the_second_read_too(native_world, native_clock):
    world = native_world
    children = [f"akb://{world.vault}/coll/hub/doc/child-{i:04d}.md" for i in range(300)]
    await world.add_doc("hub/index.md", "Hub index", "index", at=T3, related_to=children)
    await world.add_doc(B, "Beta notes", at=T1)
    await world.link("hub/index.md", B)

    counting = world.counted()
    graph = await world.graph("hub/index.md", hops=1)

    assert len(counting.label_statements()) == 2  # the wider read really happened
    assert _times(graph["nodes"]) == {world.uri("hub/index.md"): stamp(T3), world.uri(B): stamp(T1)}


async def test_a_hub_the_wider_read_cap_leaves_unread_still_states_its_time(
    native_world, native_clock, monkeypatch,
):
    """The wider read is capped in documents. Past the cap a hub is named by its
    path, but the first statement already fetched its time: the label keeps it."""
    world = native_world
    monkeypatch.setattr(kg_service, "_LABEL_RETRY_MAX_ROWS", 2)
    children = [f"akb://{world.vault}/coll/hub/doc/child-{i:04d}.md" for i in range(300)]
    hubs = [f"hub/wide-{i}.md" for i in range(5)]
    moments = {hub: T1 + timedelta(minutes=i) for i, hub in enumerate(hubs)}
    await world.add_doc(A, "Center", at=T3)
    for hub in hubs:
        await world.add_doc(hub, f"Title of {hub}", "index", at=moments[hub], related_to=children)
        await world.link(A, hub)

    counting = world.counted()
    graph = await world.graph(A, hops=1)

    assert len(counting.label_statements()) == 2  # the first read, and one capped wider read
    by_uri = {n["uri"]: n for n in graph["nodes"]}
    assert set(by_uri) == {world.uri(A), *(world.uri(hub) for hub in hubs)}
    assert {uri: n["updated_at"] for uri, n in by_uri.items()} == {
        world.uri(A): stamp(T3), **{world.uri(hub): stamp(moments[hub]) for hub in hubs},
    }
    titled = [hub for hub in hubs if by_uri[world.uri(hub)].get("title") == f"Title of {hub}"]
    unread = [hub for hub in hubs if hub not in titled]
    assert len(titled) == 2 and len(unread) == 3
    for hub in unread:  # named by the path, with everything the first read knew
        node = by_uri[world.uri(hub)]
        assert (node["name"], node["path"], "title" in node) == (hub, hub, False)


async def test_an_orphan_document_is_dated_by_the_ledger_not_by_its_frozen_row(native_world, native_clock):
    """The overview enumerates orphan documents from the catalog; on a native
    installation the row that survived cutover is frozen, so the resource that
    shares its id dates it — in the same statement. A row the ledger never
    received keeps its own time."""
    world = native_world
    await world.add_doc(A, "Alpha design", at=T1)
    await world.add_doc(B, "Beta notes", at=T1)
    await world.link(A, B)
    await world.add_doc(C, "Lonely note", at=T3)  # edited natively after cutover
    await _retain_catalog_row(world, C, "Frozen lonely note", "note", [], resource_id=world.resource_ids[C], updated_at=OLD)
    async with world.pool.acquire() as conn:  # a catalog row the ledger never received
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, tags, updated_at) "
            "VALUES ($1, $2, 'notes/never-migrated.md', 'Never migrated', 'note', '{}', $3)",
            uuid.uuid4(), world.vault_id, T2,
        )

    counting = world.counted()
    overview = await kg_service.get_overview(world.vault, vault_id=world.vault_id)

    by_uri = {n["uri"]: n for n in overview["nodes"]}
    assert by_uri[world.uri(C)]["updated_at"] == stamp(T3)  # not the frozen row's OLD
    assert by_uri[world.uri("notes/never-migrated.md")]["updated_at"] == stamp(T2)
    assert by_uri[world.uri(C)]["degree"] == 0 and overview["orphans_returned"] == 2
    # Three statements enumerate the orphans (documents, tables, files): no lookup per orphan.
    orphan_statements = [s for s in counting.statements if "ORDER BY" in s and "LIMIT" in s and "scoped_edges" not in s]
    assert len(orphan_statements) == 3, orphan_statements
