import {
  createClient,
  type AkbGraphEnvelope,
  type AkbGraphHealthEnvelope,
  type AkbGraphNeighborsEnvelope,
  type AkbGraphOverviewEnvelope,
} from "@akb/client";

const client = createClient({ baseUrl: "https://proof.invalid/api/v1", defaultVault: "proof" });
const neighbors = await client.graph.neighbors("akb://proof/doc/a", { hops: 5, limit: 17 });
const overview = await client.graph.overview({ topK: 37 });
const health = await client.graph.health({ hubThreshold: 7, limit: 11 });

const neighborsData: AkbGraphNeighborsEnvelope = neighbors.throwOnError().data;
const overviewData: AkbGraphOverviewEnvelope = overview.throwOnError().data;
const healthData: AkbGraphHealthEnvelope = health.throwOnError().data;
neighborsData.kind satisfies "graph_neighbors";
neighborsData.nodes[0]?.resource_type satisfies "doc" | "table" | "file" | undefined;
neighborsData.edges[0]?.relation satisfies "depends_on" | "related_to" | "implements" | "references" | "attached_to" | "derived_from" | "links_to" | undefined;
overviewData.kind satisfies "graph_overview";
overviewData.nodes_total satisfies number;
healthData.kind satisfies "graph_health";
healthData.orphans.sample[0]?.degree satisfies number | null | undefined;

// @ts-expect-error hops outside 1..5 are rejected.
client.graph.neighbors("akb://proof/doc/a", { hops: 0 });
// @ts-expect-error hops outside 1..5 are rejected.
client.graph.neighbors("akb://proof/doc/a", { hops: 6 });
// @ts-expect-error graph traversal uses hops, not depth.
client.graph.neighbors("akb://proof/doc/a", { depth: 2 });

const raw = await client.graph.request<AkbGraphEnvelope>("?vault=proof");
const rawData = raw.throwOnError().data;
if (rawData.kind === "graph_neighbors") {
  rawData.edges satisfies AkbGraphNeighborsEnvelope["edges"];
} else {
  rawData.orphans_returned satisfies number;
}
