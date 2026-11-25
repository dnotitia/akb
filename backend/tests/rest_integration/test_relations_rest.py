from dataclasses import dataclass

import httpx
import pytest

from tests.rest_integration.helpers import Actor, register_user

pytestmark = pytest.mark.live_services

RELATIONS = "/api/v1/relations"
OVERVIEW = "/api/v1/graph/overview"


async def test_graph_openapi_contract(api: httpx.AsyncClient):
    response = await api.get("/openapi.json")
    document = response.raise_for_status().json()
    operations = {
        ("/api/v1/graph", "get"): ("graphNeighbors", "AkbGraphEnvelope"),
        (OVERVIEW, "get"): ("graphOverview", "AkbGraphOverviewEnvelope"),
        ("/api/v1/graph/health", "get"): ("graphHealth", "AkbGraphHealthEnvelope"),
        (RELATIONS, "get"): ("graphRelations", "AkbRelationsEnvelope"),
        (RELATIONS, "post"): ("graphLink", "AkbRelationLinkEnvelope"),
        (RELATIONS, "delete"): ("graphUnlink", "AkbRelationUnlinkEnvelope"),
        ("/api/v1/provenance", "get"): ("graphProvenance", "AkbProvenanceEnvelope"),
    }
    for (path, method), (operation_id, component) in operations.items():
        operation = document["paths"][path][method]
        assert operation["operationId"] == operation_id
        assert operation["tags"] == ["graph"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{component}"
        }

    schemas = document["components"]["schemas"]
    leaves = {
        "graph_neighbors": "AkbGraphNeighborsEnvelope",
        "graph_overview": "AkbGraphOverviewEnvelope",
        "graph_health": "AkbGraphHealthEnvelope",
        "relations": "AkbRelationsEnvelope",
        "relation_link": "AkbRelationLinkEnvelope",
        "relation_unlink": "AkbRelationUnlinkEnvelope",
        "provenance": "AkbProvenanceEnvelope",
    }
    mapping = schemas["AkbSuccessEnvelope"]["discriminator"]["mapping"]
    for kind, component in leaves.items():
        assert mapping[kind] == f"#/components/schemas/{component}"
        assert "kind" in schemas[component]["required"]
        assert schemas[component]["properties"]["kind"]["enum"] == [kind]


@dataclass(frozen=True)
class Graph:
    owner: Actor
    reader: Actor
    vault: str
    other_vault: str
    source: str
    target: str
    third: str
    other: str


async def put_doc(api: httpx.AsyncClient, actor: Actor, vault: str, title: str) -> str:
    response = await api.post(
        "/api/v1/documents",
        headers=actor.headers,
        json={"vault": vault, "collection": "specs", "title": title, "content": f"# {title}"},
    )
    uri = response.raise_for_status().json()["uri"]
    assert uri
    return uri


@pytest.fixture
async def graph(api: httpx.AsyncClient):
    owner = await register_user(api, label="graph-owner")
    reader = await register_user(api, label="graph-reader")
    vault = f"graph-{owner.id.hex[:12]}"
    other_vault = f"graph-other-{owner.id.hex[:12]}"
    for name in (vault, other_vault):
        response = await api.post("/api/v1/vaults", headers=owner.headers, params={"name": name})
        response.raise_for_status()
    response = await api.post(
        f"/api/v1/vaults/{vault}/grant", headers=owner.headers, json={"user": reader.username, "role": "reader"}
    )
    assert response.raise_for_status().json()["granted"] is True
    yield Graph(
        owner,
        reader,
        vault,
        other_vault,
        source=await put_doc(api, owner, vault, "Issue Doc"),
        target=await put_doc(api, owner, vault, "Target Doc"),
        third=await put_doc(api, owner, vault, "Third Doc"),
        other=await put_doc(api, owner, other_vault, "Other Vault Doc"),
    )
    for name in (vault, other_vault):
        response = await api.delete(f"/api/v1/vaults/{name}", headers=owner.headers)
        response.raise_for_status()


async def read_relations(api: httpx.AsyncClient, graph: Graph) -> dict:
    response = await api.get(RELATIONS, headers=graph.owner.headers, params={"uri": graph.source})
    return response.raise_for_status().json()


async def test_relations_link_unlink_validation_and_canonical_uris(api: httpx.AsyncClient, graph: Graph):
    headers = graph.owner.headers
    relation = {"source": graph.source, "target": graph.target, "relation": "references"}

    for _ in range(2):
        response = await api.post(RELATIONS, headers=headers, json=relation)
        assert response.status_code == 200, response.text
        assert response.json()["linked"] is True
        assert response.json()["kind"] == "relation_link"

    relations = await read_relations(api, graph)
    assert relations["kind"] == "relations"
    assert len(relations["relations"]) == 1

    collection = f"akb://{graph.vault}/coll/specs"
    for label, payload, status in (
        ("unknown relation", {**relation, "relation": "bogus_rel"}, 422),
        ("missing target", {"source": graph.source, "relation": "references"}, 422),
        ("self-link", {**relation, "target": graph.source}, 400),
        ("cross-vault edge", {**relation, "target": graph.other}, 400),
        ("malformed source", {**relation, "source": "not-a-uri"}, 400),
        ("collection source", {**relation, "source": collection}, 400),
        ("unknown target", {**relation, "target": f"akb://{graph.vault}/doc/specs/does-not-exist"}, 404),
    ):
        response = await api.post(RELATIONS, headers=headers, json=payload)
        assert response.status_code == status, (label, response.text)

    response = await api.get(RELATIONS, headers=headers, params={"uri": graph.source, "direction": "sideways"})
    assert response.status_code == 422, response.text
    response = await api.post(RELATIONS, headers=graph.reader.headers, json={**relation, "target": graph.third})
    assert response.status_code == 403, response.text
    response = await api.post(RELATIONS, json=relation)
    assert response.status_code == 401, response.text

    response = await api.delete(RELATIONS, headers=headers, params=relation)
    deleted = response.raise_for_status().json()
    assert deleted["kind"] == "relation_unlink"
    assert deleted["unlinked"] == 1
    assert (await read_relations(api, graph))["relations"] == []

    response = await api.delete(RELATIONS, headers=headers, params=relation)
    repeated = response.raise_for_status().json()
    assert repeated["kind"] == "relation_unlink"
    assert repeated["unlinked"] == 0
    response = await api.delete(RELATIONS, headers=graph.reader.headers, params=relation)
    assert response.status_code == 403, response.text
    response = await api.delete(RELATIONS, headers=headers, params={**relation, "source": collection})
    assert response.status_code == 400, response.text
    response = await api.delete(RELATIONS, headers=headers, params={**relation, "relation": "bogus_rel"})
    assert response.status_code == 422, response.text

    for relation_type in ("references", "related_to"):
        response = await api.post(RELATIONS, headers=headers, json={**relation, "relation": relation_type})
        response.raise_for_status()
    assert len((await read_relations(api, graph))["relations"]) == 2
    response = await api.delete(RELATIONS, headers=headers, params={"source": graph.source, "target": graph.target})
    response.raise_for_status()
    assert (await read_relations(api, graph))["relations"] == []

    slash_suffix = {**relation, "source": f"{graph.source}/", "target": f"{graph.target}/"}
    response = await api.post(RELATIONS, headers=headers, json=slash_suffix)
    assert response.status_code == 200, response.text
    assert len((await read_relations(api, graph))["relations"]) >= 1
    response = await api.delete(RELATIONS, headers=headers, params=slash_suffix)
    response.raise_for_status()
    assert (await read_relations(api, graph))["relations"] == []


async def test_graph_overview_health_and_provenance(api: httpx.AsyncClient, graph: Graph):
    headers = graph.owner.headers
    params = {"vault": graph.vault}
    orphan = await put_doc(api, graph.owner, graph.vault, "Orphan Doc")
    for target in (graph.target, graph.third):
        response = await api.post(
            RELATIONS, headers=headers, json={"source": graph.source, "target": target, "relation": "depends_on"}
        )
        response.raise_for_status()

    response = await api.get("/api/v1/graph", headers=headers, params={"uri": graph.source})
    neighbors = response.raise_for_status().json()
    assert neighbors["kind"] == "graph_neighbors"
    assert {"nodes", "edges"} <= neighbors.keys()
    response = await api.get("/api/v1/graph", headers=headers, params=params)
    overview = response.raise_for_status().json()
    assert overview["kind"] == "graph_overview"
    assert {
        "nodes",
        "edges",
        "nodes_total",
        "edges_total",
        "returned",
        "truncated",
        "orphans_returned",
        "orphans_truncated",
    } <= overview.keys()

    response = await api.get("/api/v1/provenance", headers=headers, params={"uri": graph.source})
    provenance = response.raise_for_status().json()
    assert provenance["kind"] == "provenance"
    assert {
        "doc_id",
        "title",
        "path",
        "vault",
        "uri",
        "created_by",
        "created_at",
        "updated_at",
        "current_commit",
        "relations",
    } <= provenance.keys()
    assert "provenance" not in provenance

    response = await api.get(OVERVIEW, headers=headers, params={**params, "top_k": 200})
    overview = response.raise_for_status().json()
    assert overview["kind"] == "graph_overview"
    assert overview["nodes_total"] == 3
    assert overview["edges_total"] == 2
    assert overview["truncated"] is False
    hub = max(overview["nodes"], key=lambda node: node["degree"])
    assert hub["uri"] == graph.source
    assert hub["degree"] == 2
    assert {edge["kind"] for edge in overview["edges"]} == {"explicit"}
    assert overview["orphans_returned"] >= 1
    assert next(node for node in overview["nodes"] if node["uri"] == orphan)["degree"] == 0

    response = await api.get(OVERVIEW, headers=headers, params={**params, "top_k": 1})
    limited = response.raise_for_status().json()
    assert limited["returned"] == 1
    assert limited["truncated"] is True
    assert limited["nodes_total"] == 3

    response = await api.get("/api/v1/graph/health", headers=headers, params={**params, "hub_threshold": 2})
    health = response.raise_for_status().json()
    assert health["kind"] == "graph_health"
    assert any(node["uri"] == graph.source and node["degree"] == 2 for node in health["hubs"])
    assert health["orphans"]["count"] >= 1
    assert any("Orphan Doc" in (node["name"] or "") for node in health["orphans"]["sample"])

    for table in ("linked_tbl", "lonely_tbl"):
        response = await api.post(
            f"/api/v1/tables/{graph.vault}",
            headers=headers,
            json={"name": table, "columns": [{"name": "k", "type": "text"}], "collection": "specs"},
        )
        response.raise_for_status()
    response = await api.post(
        RELATIONS,
        headers=headers,
        json={
            "source": graph.source,
            "target": f"akb://{graph.vault}/coll/SPECS/table/linked_tbl",
            "relation": "references",
        },
    )
    response.raise_for_status()
    response = await api.get(OVERVIEW, headers=headers, params={**params, "top_k": 200})
    overview = response.raise_for_status().json()
    nodes = overview["nodes"]
    assert len({node["uri"] for node in nodes}) == len(nodes)
    linked = [node for node in nodes if node["uri"].endswith("/table/linked_tbl")]
    lonely = [node for node in nodes if node["uri"].endswith("/table/lonely_tbl")]
    assert len(linked) == len(lonely) == 1
    assert linked[0]["degree"] >= 1
    assert lonely[0]["degree"] == 0
    assert lonely[0]["resource_type"] == "table"
    assert len(nodes) == overview["returned"] + overview["orphans_returned"]

    response = await api.get(OVERVIEW, headers=graph.reader.headers, params=params)
    assert response.status_code == 200, response.text
    response = await api.get(OVERVIEW, headers=graph.reader.headers, params={"vault": graph.other_vault})
    assert response.status_code == 403, response.text
    response = await api.get(OVERVIEW, params=params)
    assert response.status_code == 401, response.text
