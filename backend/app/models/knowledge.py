"""Typed REST transport contracts for graph, relations, and provenance."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.kg_service import LinkRelationType
from app.util.text import NFCModel


ResourceType = Literal["doc", "table", "file"]
RelationDirection = Literal["incoming", "outgoing", "both"]
ReadRelationType = Literal[
    "depends_on",
    "related_to",
    "implements",
    "references",
    "attached_to",
    "derived_from",
    "links_to",
]
EdgeKind = Literal["implicit", "explicit"]


class LinkRequest(NFCModel):
    source: str
    target: str
    relation: LinkRelationType
    metadata: dict | None = None


class KnowledgeResponse(BaseModel):
    """Preserve additive service fields while typing the stable public shape."""

    model_config = ConfigDict(extra="allow")


class GraphNode(KnowledgeResponse):
    uri: str
    name: str
    resource_type: ResourceType
    depth: int | None = None
    degree: int | None = None


class GraphEdge(KnowledgeResponse):
    source: str
    target: str
    relation: ReadRelationType
    kind: EdgeKind


class RelationItem(KnowledgeResponse):
    direction: Literal["incoming", "outgoing"]
    relation: ReadRelationType
    uri: str
    resource_type: ResourceType
    name: str | None = None


class OrphanSummary(KnowledgeResponse):
    count: int
    sample: list[GraphNode]


class GraphNeighborsResponse(KnowledgeResponse):
    kind: Literal["graph_neighbors"] = "graph_neighbors"
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class GraphOverviewResponse(KnowledgeResponse):
    kind: Literal["graph_overview"] = "graph_overview"
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    nodes_total: int
    edges_total: int
    returned: int
    truncated: bool
    orphans_returned: int
    orphans_truncated: bool


GraphResponse = Annotated[
    GraphNeighborsResponse | GraphOverviewResponse,
    Field(discriminator="kind"),
]


class GraphHealthResponse(KnowledgeResponse):
    kind: Literal["graph_health"] = "graph_health"
    hubs: list[GraphNode]
    orphans: OrphanSummary


class RelationsResponse(KnowledgeResponse):
    kind: Literal["relations"] = "relations"
    uri: str
    relations: list[RelationItem]


class RelationLinkResponse(KnowledgeResponse):
    kind: Literal["relation_link"] = "relation_link"
    linked: bool
    source: str
    target: str
    relation: LinkRelationType


class RelationUnlinkResponse(KnowledgeResponse):
    kind: Literal["relation_unlink"] = "relation_unlink"
    unlinked: int
    source: str
    target: str


class ProvenanceResponse(KnowledgeResponse):
    kind: Literal["provenance"] = "provenance"
    doc_id: str
    title: str
    path: str
    vault: str
    uri: str
    created_by: str | None
    created_at: str | None
    updated_at: str | None
    current_commit: str | None
    relations: list[RelationItem]
