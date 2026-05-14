// frontend/src/components/graph/use-graph-data.ts
import { useQuery } from "@tanstack/react-query";
import { getGraph, getRelations, type RelationRow } from "@/lib/api";
import {
  ALL_NODE_KINDS,
  type GraphEdge,
  type GraphNode,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";

export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

const KIND_SET = new Set<string>(ALL_NODE_KINDS);

function normalizeKind(raw: string | undefined): NodeKind {
  if (raw && KIND_SET.has(raw)) return raw as NodeKind;
  return "document";
}

function normalizeRelation(raw: string | undefined): RelationKind | null {
  switch (raw) {
    case "depends_on":
    case "implements":
    case "references":
    case "related_to":
    case "attached_to":
    case "derived_from":
      return raw;
    default:
      return null;
  }
}

export function mergeGraph(a: GraphPayload, b: GraphPayload): GraphPayload {
  const nodeByUri = new Map<string, GraphNode>();
  for (const n of a.nodes) nodeByUri.set(n.uri, n);
  for (const n of b.nodes) if (!nodeByUri.has(n.uri)) nodeByUri.set(n.uri, n);
  const edgeKey = (e: GraphEdge) => `${e.source}\u0001${e.target}\u0001${e.relation}`;
  const edgeKeys = new Set<string>();
  const edges: GraphEdge[] = [];
  for (const e of [...a.edges, ...b.edges]) {
    const k = edgeKey(e);
    if (edgeKeys.has(k)) continue;
    edgeKeys.add(k);
    edges.push(e);
  }
  return { nodes: [...nodeByUri.values()], edges };
}

// react-force-graph mutates edge.source/edge.target from string-URI to a
// node-object reference once the simulation starts. Any downstream filter
// that does `keep.has(e.source)` would silently drop every edge after the
// first frame. This extractor normalizes back to the URI string.
export function endpointUri(end: unknown): string {
  if (typeof end === "string") return end;
  if (end && typeof end === "object" && "uri" in end) {
    return (end as { uri: string }).uri;
  }
  return "";
}

export function applyFilters(p: GraphPayload, v: GraphView): GraphPayload {
  const nodes = p.nodes.filter((n) => v.types.has(n.kind));
  const keep = new Set(nodes.map((n) => n.uri));
  const edges = p.edges
    .filter(
      (e) =>
        v.relations.has(e.relation) &&
        keep.has(endpointUri(e.source)) &&
        keep.has(endpointUri(e.target)),
    )
    // Freshen each edge so force-graph mutating source/target on this
    // render's links doesn't poison the cached base graph for the next
    // filter toggle. Reset to plain URI strings so the simulation can
    // re-resolve them against the current node objects.
    .map((e) => ({
      source: endpointUri(e.source),
      target: endpointUri(e.target),
      relation: e.relation,
    }));
  return { nodes, edges };
}

export const DEGRADED_NODE_THRESHOLD = 500;

export function isDegraded(rawNodeCount: number): boolean {
  return rawNodeCount > DEGRADED_NODE_THRESHOLD;
}

interface BfsExpandArgs {
  vault: string;
  entry: string; // doc_id
  depth: 1 | 2 | 3;
  fetchRelations: (
    vault: string,
    docId: string,
  ) => Promise<{ resource_uri: string; relations: RelationRow[] }>;
}

function rowToEdge(row: RelationRow, selfUri: string): GraphEdge | null {
  const relation = normalizeRelation(row.relation);
  if (!relation) return null;
  if (row.direction === "outgoing") {
    return { source: selfUri, target: row.uri, relation };
  }
  return { source: row.uri, target: selfUri, relation };
}

function rowToNeighbor(row: RelationRow): GraphNode | null {
  if (!row.uri) return null;
  return {
    uri: row.uri,
    name: row.name || row.uri,
    kind: normalizeKind(row.resource_type),
  };
}

// Extract the doc / table / file id from an `akb://{vault}/{kind}/{path}` URI.
// Multi-segment paths (e.g. `specs/2026/foo.md`) are preserved intact —
// `find_by_ref` on the backend matches the metadata `id` or the `path LIKE`
// fallback against this full tail.
export function docIdFromUri(uri: string): string | null {
  const m = uri.match(/^akb:\/\/[^/]+\/(?:doc|table|file)\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

export async function bfsExpand(args: BfsExpandArgs): Promise<GraphPayload> {
  const { vault, entry, depth, fetchRelations } = args;
  const visited = new Set<string>();
  visited.add(entry);

  const seedResp = await fetchRelations(vault, entry);
  const seedNode: GraphNode = {
    uri: seedResp.resource_uri,
    name: entry,
    kind: "document",
    doc_id: entry,
  };
  const nodesByUri = new Map<string, GraphNode>([[seedNode.uri, seedNode]]);
  const edgeKeys = new Set<string>();
  const edges: GraphEdge[] = [];

  function ingest(rows: RelationRow[], selfUri: string): string[] {
    const newDocIds: string[] = [];
    for (const row of rows) {
      const edge = rowToEdge(row, selfUri);
      if (edge) {
        const k = `${edge.source}\u0001${edge.target}\u0001${edge.relation}`;
        if (!edgeKeys.has(k)) {
          edgeKeys.add(k);
          edges.push(edge);
        }
      }
      const neighbor = rowToNeighbor(row);
      if (neighbor && !nodesByUri.has(neighbor.uri)) {
        nodesByUri.set(neighbor.uri, neighbor);
        const id = docIdFromUri(neighbor.uri);
        if (id && !visited.has(id)) newDocIds.push(id);
      }
    }
    return newDocIds;
  }

  // Seed hop populates the first frontier from the entry's neighbors.
  // Dedup is enforced inside the loop's `toFetch` filter (visited.add
  // claims the docId atomically), so duplicate entries in `frontier`
  // — e.g. siblings pointing at the same neighbor — collapse to a
  // single fetch on the next hop.
  let frontier: string[] = ingest(seedResp.relations, seedResp.resource_uri);

  for (let hop = 1; hop < depth; hop++) {
    const toFetch = frontier.filter((docId) => {
      if (visited.has(docId)) return false;
      visited.add(docId);
      return true;
    });
    if (toFetch.length === 0) break;
    const responses = await Promise.all(
      toFetch.map((docId) => fetchRelations(vault, docId).catch(() => null)),
    );
    const nextFrontier: string[] = [];
    for (const r of responses) {
      if (!r) continue;
      nextFrontier.push(...ingest(r.relations, r.resource_uri));
    }
    if (nextFrontier.length === 0) break;
    frontier = nextFrontier;
  }

  return { nodes: [...nodesByUri.values()], edges };
}

export function useFullGraph(vault: string, enabled: boolean) {
  return useQuery({
    queryKey: ["graph", vault, "full"],
    enabled,
    queryFn: async (): Promise<GraphPayload> => {
      // Full mode loads up to 200 nodes; `isDegraded` (>500) is unreachable
      // today, so 200 is the practical upper bound on full-vault renders.
      // Truncation is silent — surface "showing first N of M" if it becomes
      // a real concern.
      const resp = await getGraph(vault, undefined, 2, 200);
      const nodes: GraphNode[] = resp.nodes.map((n) => ({
        uri: n.uri,
        name: n.name || n.uri,
        kind: normalizeKind(n.resource_type),
      }));
      const edges: GraphEdge[] = resp.edges
        .map((e) => {
          const rel = normalizeRelation(e.relation);
          return rel ? { source: e.source, target: e.target, relation: rel } : null;
        })
        .filter((e): e is GraphEdge => e !== null);
      return { nodes, edges };
    },
  });
}

export function useNeighborhood(
  vault: string,
  entry: string | undefined,
  depth: 1 | 2 | 3,
) {
  return useQuery({
    queryKey: ["graph", vault, "neighborhood", entry, depth],
    enabled: !!entry,
    queryFn: () =>
      bfsExpand({
        vault,
        // `enabled: !!entry` above gates this query; entry is defined here.
        entry: entry!,
        depth,
        fetchRelations: async (v, docId) => {
          const r = await getRelations(v, docId);
          return { resource_uri: r.resource_uri, relations: r.relations };
        },
      }),
  });
}
