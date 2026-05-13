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

export function applyFilters(p: GraphPayload, v: GraphView): GraphPayload {
  const nodes = p.nodes.filter((n) => v.types.has(n.kind));
  const keep = new Set(nodes.map((n) => n.uri));
  const edges = p.edges.filter(
    (e) => v.relations.has(e.relation) && keep.has(e.source) && keep.has(e.target),
  );
  return { nodes, edges };
}

export function isDegraded(rawNodeCount: number): boolean {
  return rawNodeCount > 500;
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

function rowToEdge(row: RelationRow): GraphEdge | null {
  const relation = normalizeRelation(row.relation);
  if (!relation) return null;
  return { source: row.source, target: row.target, relation };
}

function rowToNeighbor(row: RelationRow): GraphNode | null {
  if (!row.other_uri) return null;
  return {
    uri: row.other_uri,
    name: row.other_name || row.other_uri,
    kind: normalizeKind(row.other_type),
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

  function ingest(rows: RelationRow[]): string[] {
    const newDocIds: string[] = [];
    for (const row of rows) {
      const edge = rowToEdge(row);
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
  let frontier: string[] = ingest(seedResp.relations);

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
      nextFrontier.push(...ingest(r.relations));
    }
    if (nextFrontier.length === 0) break;
    frontier = nextFrontier;
  }

  return { nodes: [...nodesByUri.values()], edges };
}

/* React Query hooks — the consumers used by `pages/graph.tsx`. */

export function useFullGraph(vault: string, enabled: boolean) {
  return useQuery({
    queryKey: ["graph", vault, "full"],
    enabled,
    queryFn: async (): Promise<GraphPayload> => {
      const resp = await getGraph(vault);
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
        entry: entry as string,
        depth,
        fetchRelations: async (v, docId) => {
          const r = await getRelations(v, docId);
          return { resource_uri: r.resource_uri, relations: r.relations };
        },
      }),
  });
}
