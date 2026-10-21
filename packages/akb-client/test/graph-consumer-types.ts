import {
  createClient,
  type AkbGraphDirection,
  type AkbGraphEdge,
  type AkbGraphEnvelope,
  type AkbGraphHealthEnvelope,
  type AkbGraphNeighborsEnvelope,
  type AkbGraphNode,
  type AkbGraphOverviewEnvelope,
} from "@akb/client";

const client = createClient({ baseUrl: "https://proof.invalid/api/v1", defaultVault: "proof" });
const neighbors = await client.graph.neighbors("akb://proof/doc/a", { hops: 5, limit: 17 });
const strongNeighbors = await client.graph.neighbors("akb://proof/doc/a", { edgeType: "strong" });
const outwardNeighbors = await client.graph.neighbors("akb://proof/doc/a", { edgeType: "strong", direction: "outward" });
const bothNeighbors = await client.graph.neighbors("akb://proof/doc/a", { direction: "both" });
const overview = await client.graph.overview({ topK: 37 });
const health = await client.graph.health({ hubThreshold: 7, limit: 11 });

const neighborsData: AkbGraphNeighborsEnvelope = neighbors.throwOnError().data;
const overviewData: AkbGraphOverviewEnvelope = overview.throwOnError().data;
const healthData: AkbGraphHealthEnvelope = health.throwOnError().data;
neighborsData.kind satisfies "graph_neighbors";
neighborsData.truncated satisfies boolean | undefined;
strongNeighbors.throwOnError().data.edges[0]?.edge_type satisfies "strong" | "weak" | undefined;
// The walk direction composes with the strength and answers with the same envelope.
outwardNeighbors.throwOnError().data satisfies AkbGraphNeighborsEnvelope;
bothNeighbors.throwOnError().data.kind satisfies "graph_neighbors";
({ direction: "outward" }) satisfies { direction: AkbGraphDirection };
// A node is named by its title; a document also says what it is and where it lives.
neighborsData.nodes[0]?.name satisfies string | undefined;
neighborsData.nodes[0]?.title satisfies string | undefined;
neighborsData.nodes[0]?.doc_type satisfies string | undefined;
neighborsData.nodes[0]?.tags satisfies string[] | undefined;
neighborsData.nodes[0]?.path satisfies string | undefined;
// When the resource last changed (UTC); a node whose store has no time states none.
neighborsData.nodes[0]?.updated_at satisfies string | undefined;
neighborsData.nodes[0]?.resource_type satisfies "doc" | "table" | "file" | undefined;
neighborsData.edges[0]?.relation satisfies "depends_on" | "related_to" | "implements" | "references" | "attached_to" | "derived_from" | "links_to" | undefined;
overviewData.kind satisfies "graph_overview";
overviewData.nodes_total satisfies number;
healthData.kind satisfies "graph_health";
healthData.orphans.sample[0]?.degree satisfies number | null | undefined;

// The SDK returns a response unchanged, and a server that predates a field never
// sends it. What such a server answers has to be a value of these types, and
// using one of these fields without checking it first has to be an error: every
// line here compiles only while the field is optional.
const olderNode: AkbGraphNode = { uri: "akb://proof/doc/a", name: "a", resource_type: "doc" };
const olderEdge: AkbGraphEdge = {
  source: "akb://proof/doc/a",
  target: "akb://proof/doc/b",
  relation: "related_to",
  kind: "implicit",
};
const olderNeighbors: AkbGraphNeighborsEnvelope = {
  kind: "graph_neighbors",
  nodes: [olderNode],
  edges: [olderEdge],
};
// @ts-expect-error an older server sends no edge_type, so it has to be checked before it is used.
olderEdge.edge_type.toUpperCase();
// @ts-expect-error title is absent for a document without one and from an older server.
olderNode.title.length;
// @ts-expect-error doc_type is absent from an older server.
olderNode.doc_type.length;
// @ts-expect-error tags are absent from an older server.
olderNode.tags.length;
// @ts-expect-error path is absent from an older server.
olderNode.path.length;
// @ts-expect-error updated_at is absent from an older server and when the store has no time.
olderNode.updated_at.length;
// @ts-expect-error truncated is absent from an older server's neighbourhood.
olderNeighbors.truncated.valueOf();
olderEdge.edge_type satisfies "strong" | "weak" | undefined;

// @ts-expect-error hops outside 1..5 are rejected.
client.graph.neighbors("akb://proof/doc/a", { hops: 0 });
// @ts-expect-error hops outside 1..5 are rejected.
client.graph.neighbors("akb://proof/doc/a", { hops: 6 });
// @ts-expect-error edgeType is strong or weak.
client.graph.neighbors("akb://proof/doc/a", { edgeType: "medium" });
// @ts-expect-error the wire name is for the server; the option is edgeType.
client.graph.neighbors("akb://proof/doc/a", { edge_type: "strong" });
// @ts-expect-error direction is both or outward; the relations vocabulary does not apply to a walk.
client.graph.neighbors("akb://proof/doc/a", { direction: "incoming" });
// @ts-expect-error graph traversal uses hops, not depth.
client.graph.neighbors("akb://proof/doc/a", { depth: 2 });

const raw = await client.graph.request<AkbGraphEnvelope>("?vault=proof");
const rawData = raw.throwOnError().data;
if (rawData.kind === "graph_neighbors") {
  rawData.edges satisfies AkbGraphNeighborsEnvelope["edges"];
} else {
  rawData.orphans_returned satisfies number;
}
