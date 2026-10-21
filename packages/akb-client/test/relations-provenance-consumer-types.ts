import {
  createClient,
  type AkbEdgeType,
  type AkbGraphRelationsOptions,
  type AkbProvenanceEnvelope,
  type AkbRelation,
  type AkbRelationLinkEnvelope,
  type AkbRelationType,
  type AkbRelationUnlinkEnvelope,
  type AkbRelationsEnvelope,
  type AkbWritableRelationType,
  type LinkRequest,
} from "@akb/client";

const client = createClient({ baseUrl: "https://proof.invalid/api/v1" });
const readRelation: AkbRelationType = "links_to";
const writeRelation: AkbWritableRelationType = "references";
const options: AkbGraphRelationsOptions = { direction: "both", type: readRelation };
const relations: AkbRelationsEnvelope = (await client.graph.relations("akb://proof/doc/a", options)).throwOnError().data;
const row: AkbRelation | undefined = relations.relations[0];
row?.relation satisfies AkbRelationType | undefined;
row?.name satisfies string | null | undefined;
row?.edge_type satisfies AkbEdgeType | undefined;
// The row names the resource at the other end by its title; documents add the rest.
row?.title satisfies string | undefined;
row?.doc_type satisfies string | undefined;
row?.tags satisfies string[] | undefined;
row?.path satisfies string | undefined;
row?.updated_at satisfies string | undefined;

const linked: AkbRelationLinkEnvelope = (await client.graph.link({
  source: "akb://proof/doc/a",
  target: "akb://proof/table/b",
  relation: writeRelation,
  metadata: { proof: true },
})).throwOnError().data;
linked.kind satisfies "relation_link";
linked.edge_type satisfies AkbEdgeType | undefined;

const weakLinked: AkbRelationLinkEnvelope = (await client.graph.link({
  source: "akb://proof/doc/a",
  target: "akb://proof/doc/b",
  relation: writeRelation,
  edge_type: "weak",
})).throwOnError().data;
weakLinked.edge_type satisfies AkbEdgeType | undefined;

const unlinked: AkbRelationUnlinkEnvelope = (await client.graph.unlink({
  source: "akb://proof/doc/a",
  target: "akb://proof/table/b",
})).throwOnError().data;
unlinked.kind satisfies "relation_unlink";

const provenance: AkbProvenanceEnvelope = (await client.graph.provenance("akb://proof/doc/a")).throwOnError().data;
provenance.kind satisfies "provenance";
provenance.created_by satisfies string | null;
provenance.current_commit satisfies string | null;
provenance.relations satisfies AkbRelation[];

// The SDK returns a response unchanged, and a server that predates a field never
// sends it. What such a server answers has to be a value of these types, and
// using one of these fields without checking it first has to be an error: every
// line here compiles only while the field is optional.
const olderRow: AkbRelation = {
  direction: "outgoing",
  relation: "related_to",
  uri: "akb://proof/doc/b",
  resource_type: "doc",
};
const olderLink: AkbRelationLinkEnvelope = {
  kind: "relation_link",
  linked: true,
  source: "akb://proof/doc/a",
  target: "akb://proof/doc/b",
  relation: "references",
};
// @ts-expect-error an older server sends no edge_type, so it has to be checked before it is used.
olderRow.edge_type.toUpperCase();
// @ts-expect-error title is absent for a document without one and from an older server.
olderRow.title.length;
// @ts-expect-error doc_type is absent from an older server.
olderRow.doc_type.length;
// @ts-expect-error tags are absent from an older server.
olderRow.tags.length;
// @ts-expect-error path is absent from an older server.
olderRow.path.length;
// @ts-expect-error updated_at is absent from an older server and when the store has no time.
olderRow.updated_at.length;
// @ts-expect-error an older server's link result carries no edge_type.
olderLink.edge_type.toUpperCase();

// The wire body leaves edge_type out, or sends null, to say nothing about strength
// (a new link is strong; an existing one keeps its strength). Naming it sets it.
({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "references" }) satisfies LinkRequest;
({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "references", edge_type: null }) satisfies LinkRequest;
({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "references", edge_type: "weak" }) satisfies LinkRequest;
// @ts-expect-error edge_type is strong or weak (or null for "not specified").
({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "references", edge_type: "medium" }) satisfies LinkRequest;

// @ts-expect-error links_to is read-only and cannot be explicitly linked.
client.graph.link({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "links_to" });
// @ts-expect-error edge_type is strong or weak.
client.graph.link({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "references", edge_type: "medium" });
// @ts-expect-error links_to cannot be a named unlink relation; omit relation to remove all edges.
client.graph.unlink({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "links_to" });
// @ts-expect-error links_to is outside the writable relation vocabulary.
const invalidWriteRelation: AkbWritableRelationType = "links_to";
