import {
  createClient,
  type AkbGraphRelationsOptions,
  type AkbProvenanceEnvelope,
  type AkbRelation,
  type AkbRelationLinkEnvelope,
  type AkbRelationType,
  type AkbRelationUnlinkEnvelope,
  type AkbRelationsEnvelope,
  type AkbWritableRelationType,
} from "@akb/client";

const client = createClient({ baseUrl: "https://proof.invalid/api/v1" });
const readRelation: AkbRelationType = "links_to";
const writeRelation: AkbWritableRelationType = "references";
const options: AkbGraphRelationsOptions = { direction: "both", type: readRelation };
const relations: AkbRelationsEnvelope = (await client.graph.relations("akb://proof/doc/a", options)).throwOnError().data;
const row: AkbRelation | undefined = relations.relations[0];
row?.relation satisfies AkbRelationType | undefined;
row?.name satisfies string | null | undefined;

const linked: AkbRelationLinkEnvelope = (await client.graph.link({
  source: "akb://proof/doc/a",
  target: "akb://proof/table/b",
  relation: writeRelation,
  metadata: { proof: true },
})).throwOnError().data;
linked.kind satisfies "relation_link";

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

// @ts-expect-error links_to is read-only and cannot be explicitly linked.
client.graph.link({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "links_to" });
// @ts-expect-error links_to cannot be a named unlink relation; omit relation to remove all edges.
client.graph.unlink({ source: "akb://proof/doc/a", target: "akb://proof/doc/b", relation: "links_to" });
// @ts-expect-error links_to is outside the writable relation vocabulary.
const invalidWriteRelation: AkbWritableRelationType = "links_to";
