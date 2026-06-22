// frontend/src/components/graph/use-graph-data.ts
import { useQuery } from "@tanstack/react-query";
import {
  getGraph,
  getRelations,
  type GraphApiNode,
  type GraphApiEdge,
  type RelationRow,
} from "@/lib/api";
import { parseUri } from "@/lib/uri";
import { groupOf } from "./cluster";
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
    case "links_to":
      return raw;
    default:
      return null;
  }
}

/** Convert a backend /graph response into the renderer's payload shape.
 *  Shared by the full-graph load and on-demand node expansion so both map
 *  kinds/relations/groups identically. */
export function apiToPayload(resp: { nodes: GraphApiNode[]; edges: GraphApiEdge[] }): GraphPayload {
  const nodes: GraphNode[] = resp.nodes.map((n) => ({
    uri: n.uri,
    name: n.name || n.uri,
    kind: normalizeKind(n.resource_type),
    group: groupOf(n.uri),
  }));
  const edges: GraphEdge[] = resp.edges
    .map((e) => {
      const rel = normalizeRelation(e.relation);
      return rel ? { source: e.source, target: e.target, relation: rel } : null;
    })
    .filter((e): e is GraphEdge => e !== null);
  return { nodes, edges };
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

/** uri → degree (incident visible-edge count). Endpoints are normalized via
 *  endpointUri so it's correct whether the sim has mutated source/target from
 *  URI strings to node objects yet. Shared by the canvas (node sizing + LOD
 *  ranking) and the page (hubs / sr-only list) so the degree rule lives once. */
export function degreeMap(edges: GraphEdge[]): Map<string, number> {
  const d = new Map<string, number>();
  for (const e of edges) {
    const s = endpointUri(e.source);
    const t = endpointUri(e.target);
    d.set(s, (d.get(s) ?? 0) + 1);
    d.set(t, (d.get(t) ?? 0) + 1);
  }
  return d;
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

interface BfsExpandArgs {
  vault: string;
  entry: string; // doc path within the vault
  // BFS traversal radius in edge hops. Named to match the backend's
  // `akb_graph.hops` — same concept (graph traversal distance),
  // just at different layers (this function makes `hops` round
  // trips through `akb_relations`; the backend graph endpoint
  // does its own BFS in a single round trip).
  hops: 1 | 2 | 3;
  fetchRelations: (
    vault: string,
    docPath: string,
  ) => Promise<{ uri: string; relations: RelationRow[] }>;
}

function rowToEdge(row: RelationRow, selfUri: string): GraphEdge | null {
  const relation = normalizeRelation(row.relation);
  if (!relation) return null;
  // The relations endpoint can in principle emit an edge whose
  // counter-party URI is null (unresolved forward ref). Skipping
  // those keeps the graph free of `{source: "...", target: undefined}`
  // shapes that would silently confuse the layout / viz layer.
  if (!row.uri) return null;
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
    group: groupOf(row.uri),
  };
}

// Extract the doc / table / file id from an `akb://{vault}/{kind}/{path}` URI.
// Multi-segment paths (e.g. `specs/2026/foo.md`) are preserved intact —
// `find_by_ref` on the backend matches the metadata `id` or the `path LIKE`
// fallback against this full tail.
function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/**
 * Resolve the backend lookup identifier from a resource URI.
 *
 * Thin adapter over `parseUri` in `lib/uri.ts` — the single source of
 * truth for the AKB URI scheme (it mirrors backend `uri_service.py`).
 * `parseUri().id` already yields the right identifier per kind:
 *  - doc   → the document *path* within the vault (`collPath/basename`)
 *  - table → the table name
 *  - file  → the file id (uuid)
 *
 * We add only what's specific to backend lookups: restrict to addressable
 * resource kinds (vault/coll URIs carry no document id → null) and
 * percent-decode, since `getDocument`/`getRelations`/… expect the decoded
 * path. Delegating keeps graph node selection + BFS expansion from drifting
 * when the scheme evolves — the prior hand-rolled regex only matched the
 * legacy root shape, so collection-scoped docs (the common case) resolved
 * to null and the detail panel never opened.
 */
export function docIdFromUri(uri: string): string | null {
  const parsed = parseUri(uri);
  if (!parsed || (parsed.kind !== "doc" && parsed.kind !== "table" && parsed.kind !== "file")) {
    return null;
  }
  // parseUri preserves raw URI encoding; decode for the backend lookup.
  return safeDecode(parsed.id);
}

export async function bfsExpand(args: BfsExpandArgs): Promise<GraphPayload> {
  const { vault, entry, hops, fetchRelations } = args;
  const visited = new Set<string>();
  visited.add(entry);

  const seedResp = await fetchRelations(vault, entry);
  const seedNode: GraphNode = {
    uri: seedResp.uri,
    name: entry,
    kind: "document",
    doc_id: entry,
    group: groupOf(seedResp.uri),
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
  let frontier: string[] = ingest(seedResp.relations, seedResp.uri);

  for (let hop = 1; hop < hops; hop++) {
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
      nextFrontier.push(...ingest(r.relations, r.uri));
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
      // Loads the whole vault graph. The 200 here is the backend BFS relation
      // fan-out `limit` (per traversal), NOT a cap on |V| — large vaults return
      // hundreds/thousands of nodes, which the canvas handles via its layout
      // perf tiers (GraphCanvas.layoutTier), not a render gate.
      const resp = await getGraph(vault, undefined, 2, 200);
      return apiToPayload(resp);
    },
  });
}

/** Fetch one node's immediate neighborhood via the backend graph BFS (single
 *  round trip), for on-demand expand. Returns a payload to merge into the
 *  session overlay. */
export async function fetchNeighbors(
  vault: string,
  id: string,
  hops: 1 | 2 = 1,
): Promise<GraphPayload> {
  const resp = await getGraph(vault, id, hops, 100);
  return apiToPayload(resp);
}

export function useNeighborhood(
  vault: string,
  entry: string | undefined,
  hops: 1 | 2 | 3,
) {
  return useQuery({
    queryKey: ["graph", vault, "neighborhood", entry, hops],
    enabled: !!entry,
    queryFn: () =>
      bfsExpand({
        vault,
        // `enabled: !!entry` above gates this query; entry is defined here.
        entry: entry!,
        hops,
        fetchRelations: async (v, docPath) => {
          const r = await getRelations(v, docPath);
          return { uri: r.uri, relations: r.relations };
        },
      }),
  });
}
