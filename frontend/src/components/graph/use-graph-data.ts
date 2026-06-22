// frontend/src/components/graph/use-graph-data.ts
import { useQuery } from "@tanstack/react-query";
import {
  getGraph,
  getGraphOverview,
  type GraphApiNode,
  type GraphApiEdge,
} from "@/lib/api";
import { parseUri } from "@/lib/uri";
import { groupOf } from "./cluster";
import {
  ALL_NODE_KINDS,
  RELATION_CLASS,
  type GraphEdge,
  type GraphNode,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";

/** Honest counts behind a possibly-truncated overview, so the UI can render
 *  "showing N of M" instead of silently capping. Only the full-vault overview
 *  load sets this; expansions/filters carry the base load's meta through. */
export interface GraphMeta {
  nodesTotal: number;
  edgesTotal: number;
  returned: number;
  truncated: boolean;
}

export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
  meta?: GraphMeta;
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
  // `b` is an expansion overlay merged onto base `a`; the base's totals still
  // describe the whole vault, so carry them (not b's neighborhood counts).
  return { nodes: [...nodeByUri.values()], edges, meta: a.meta };
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

/** Directed impact cones from `selected` over STRUCTURAL edges only:
 *  `out` = its DEPENDENCIES (reachable by following source→target),
 *  `in`  = its DEPENDENTS  (reachable by following target→source).
 *  The root is excluded from both; cycle-safe (visited guard). Pure helper for
 *  the impact-analysis view — endpoints normalized via endpointUri. */
export function impactCones(
  edges: GraphEdge[],
  selected: string,
): { out: Set<string>; in: Set<string> } {
  const fwd = new Map<string, string[]>();
  const bwd = new Map<string, string[]>();
  const push = (m: Map<string, string[]>, k: string, v: string) => {
    const a = m.get(k);
    if (a) a.push(v);
    else m.set(k, [v]);
  };
  for (const e of edges) {
    if (RELATION_CLASS[e.relation] !== "structural") continue;
    const s = endpointUri(e.source);
    const t = endpointUri(e.target);
    push(fwd, s, t);
    push(bwd, t, s);
  }
  const bfs = (start: string, adj: Map<string, string[]>) => {
    const seen = new Set<string>();
    const queue = [start];
    while (queue.length) {
      const u = queue.shift() as string;
      for (const v of adj.get(u) ?? []) {
        if (v !== start && !seen.has(v)) {
          seen.add(v);
          queue.push(v);
        }
      }
    }
    return seen;
  };
  return { out: bfs(selected, fwd), in: bfs(selected, bwd) };
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
  return { nodes, edges, meta: p.meta };
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

export function useFullGraph(vault: string, enabled: boolean) {
  return useQuery({
    queryKey: ["graph", vault, "overview"],
    enabled,
    queryFn: async (): Promise<GraphPayload> => {
      // Degree-ranked overview. `top_k` (200) keeps the highest-degree nodes
      // plus the edges induced among them, with honest totals so the UI can
      // show "showing N of M" instead of the old arbitrary recency cap.
      const resp = await getGraphOverview(vault, 200);
      return {
        ...apiToPayload(resp),
        meta: {
          nodesTotal: resp.nodes_total,
          edgesTotal: resp.edges_total,
          returned: resp.returned,
          truncated: resp.truncated,
        },
      };
    },
  });
}

/** Fetch one node's immediate neighborhood via the backend graph BFS (single
 *  round trip), for on-demand expand. Returns a payload to merge into the
 *  session overlay. */
export async function fetchNeighbors(
  vault: string,
  id: string,
  hops: 1 | 2 | 3 = 1,
  limit = 100,
): Promise<GraphPayload> {
  const resp = await getGraph(vault, id, hops, limit);
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
    // Single server-side BFS call (was N per-node /relations round trips).
    // `enabled: !!entry` gates this query; entry is defined here.
    queryFn: () => fetchNeighbors(vault, entry!, hops, 200),
  });
}
