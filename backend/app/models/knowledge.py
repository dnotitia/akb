"""Typed REST transport contracts for graph, relations, and provenance."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.kg_service import EdgeType, LinkRelationType
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
    # No advertised default, and `null` is the same as leaving it out. A request
    # that names no strength says nothing about it: a new edge is strong, an
    # existing one keeps its strength. A default of `strong` here would turn every
    # bare re-link (a client that predates the field, one updating only `metadata`)
    # into a promotion, and a generator that fills defaults would do the same.
    edge_type: EdgeType | None = Field(
        default=None,
        description=(
            "Connection strength: strong or weak; body links are always weak. "
            "Omit it to keep an existing link's strength (a new link is strong). "
            "Stored under the reserved `edge_type` key of `metadata`."
        ),
    )


class KnowledgeResponse(BaseModel):
    """Preserve additive service fields while typing the stable public shape."""

    model_config = ConfigDict(extra="allow")


class GraphNode(KnowledgeResponse):
    """A resource in a graph. ``name`` is its title when it has one.

    ``title`` / ``doc_type`` / ``tags`` / ``path`` are set only when the
    resource resolves in the vault: a document carries all four (``title``
    only if it has a real one), a table or file just ``title``.
    ``updated_at`` (UTC, ``YYYY-MM-DDTHH:MM:SSZ``) says when the resource last
    changed and is left out when that is unknown.
    """

    uri: str
    name: str
    resource_type: ResourceType
    depth: int | None = None
    degree: int | None = None
    title: str | None = None
    doc_type: str | None = None
    tags: list[str] | None = None
    path: str | None = None
    updated_at: str | None = None


class GraphEdge(KnowledgeResponse):
    source: str
    target: str
    relation: ReadRelationType
    kind: EdgeKind
    edge_type: EdgeType


class RelationItem(KnowledgeResponse):
    direction: Literal["incoming", "outgoing"]
    relation: ReadRelationType
    uri: str
    resource_type: ResourceType
    kind: EdgeKind
    edge_type: EdgeType
    # The resource at the other end: ``name`` is its title when it has one, and
    # the rest follow `GraphNode` (set only when the resource resolves).
    name: str | None = None
    title: str | None = None
    doc_type: str | None = None
    tags: list[str] | None = None
    path: str | None = None
    updated_at: str | None = None


class OrphanSummary(KnowledgeResponse):
    count: int
    sample: list[GraphNode]


class GraphNeighborsResponse(KnowledgeResponse):
    kind: Literal["graph_neighbors"] = "graph_neighbors"
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    # True when the `limit` node cap cut the walk short: more nodes qualified
    # within `hops` than were returned. Parallel to the overview's `truncated`.
    truncated: bool | None = None


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
    edge_type: EdgeType


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
