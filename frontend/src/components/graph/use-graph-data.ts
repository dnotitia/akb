// frontend/src/components/graph/use-graph-data.ts
import { useQuery } from "@tanstack/react-query";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import {
  getGraph,
  getGraphOverview,
  type GraphOverviewResponse,
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

/** Overview counts are server evidence, never inferred from the scene size.
 *  nodesTotal/returned/truncated describe only CONNECTED resources; orphan
 *  resources are additional. Missing fields from older servers mean unknown.
 *  Expansions and filters carry the base load's metadata through unchanged. */
export interface GraphMeta {
  nodesTotal?: number;
  edgesTotal?: number;
  returned?: number;
  truncated?: boolean;
  orphanReturned?: number;
  orphanTruncated?: boolean;
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
export function apiToPayload(resp: GraphOverviewResponse): GraphPayload {
  const nodes: GraphNode[] = resp.nodes.map((n) => ({
    uri: n.uri,
    name: n.name || n.uri,
    kind: normalizeKind(n.resource_type),
    group: groupOf(n.uri),
    degree: n.degree ?? undefined,
    depth: n.depth ?? undefined,
  }));
  const edges: GraphEdge[] = resp.edges
    .map((e): GraphEdge | null => {
      const rel = normalizeRelation(e.relation);
      return rel ? { source: endpointUri(e.source), target: endpointUri(e.target), relation: rel, kind: e.kind } : null;
    })
    .filter((e): e is GraphEdge => e !== null);
  const meta: GraphMeta = {
    nodesTotal: resp.nodes_total,
    edgesTotal: resp.edges_total,
    returned: resp.returned,
    truncated: resp.truncated,
    orphanReturned: resp.orphans_returned,
    orphanTruncated: resp.orphans_truncated,
  };
  return { nodes, edges, ...(Object.values(meta).some((value) => value !== undefined) ? { meta } : {}) };
}

export function mergeGraph(a: GraphPayload, b: GraphPayload): GraphPayload {
  const nodeByUri = new Map<string, GraphNode>();
  for (const n of a.nodes) nodeByUri.set(n.uri, n);
  for (const n of b.nodes) if (!nodeByUri.has(n.uri)) nodeByUri.set(n.uri, n);
  const edgeKey = (e: GraphEdge) => JSON.stringify([
    endpointUri(e.source), endpointUri(e.target), e.relation, e.kind,
  ]);
  const edgeKeys = new Set<string>();
  const edges: GraphEdge[] = [];
  for (const e of [...a.edges, ...b.edges]) {
    const k = edgeKey(e);
    if (edgeKeys.has(k)) continue;
    edgeKeys.add(k);
    edges.push({ ...e, source: endpointUri(e.source), target: endpointUri(e.target) });
  }
  // `b` is an expansion overlay merged onto base `a`; the base's totals still
  // describe the whole vault, so carry them (not b's neighborhood counts).
  return { nodes: [...nodeByUri.values()], edges, meta: a.meta };
}

// Legacy graph payloads may carry node-object endpoint references. Normalize
// them to canonical URI strings for matching, merging, and filtering.
export function endpointUri(end: unknown): string {
  if (typeof end === "string") return end;
  if (end && typeof end === "object" && "uri" in end) {
    return (end as { uri: string }).uri;
  }
  return "";
}

/** uri → degree (incident visible-edge count). Normalizes legacy object
 *  endpoints through endpointUri. Shared by scene labels and page hub ranking
 *  so the visible-degree rule lives once. */
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
  return visibleGraph(p, v, new Set(), false);
}

/** The shared Canvas/List scene. Only server degree=0 establishes that a
 *  resource is unlinked: a missing visible edge may be filtered or not loaded.
 *  Clone scene inputs so renderer-owned changes cannot leak into query-cache
 *  data or another view of the same payload. */
export function visibleGraph(
  p: GraphPayload,
  v: GraphView,
  hidden: Set<string>,
  hideOrphans: boolean,
): GraphPayload {
  const nodes = p.nodes
    .filter((n) => v.types.has(n.kind) && !hidden.has(n.uri) && !(hideOrphans && n.degree === 0))
    .map((n) => ({ ...n }));
  const keep = new Set(nodes.map((n) => n.uri));
  const edges = p.edges
    .filter(
      (e) =>
        v.relations.has(e.relation) &&
        keep.has(endpointUri(e.source)) &&
        keep.has(endpointUri(e.target)),
    )
    // Keep each scene independent and normalize legacy object endpoints to
    // canonical URI strings before giving them to the renderer.
    .map((e) => ({
      ...e,
      source: endpointUri(e.source),
      target: endpointUri(e.target),
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
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return useQuery({
    queryKey: ["graph", user?.user_id, revision, vault, "overview"],
    enabled: enabled && !!user?.user_id && !checking,
    queryFn: async (): Promise<GraphPayload> => {
      // Degree-ranked overview. `top_k` (200) keeps the highest-degree nodes
      // plus the edges induced among them, with honest totals so the UI can
      // show "showing N of M" instead of the old arbitrary recency cap.
      const resp = await getGraphOverview(vault, 200);
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
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return useQuery({
    queryKey: ["graph", user?.user_id, revision, vault, "neighborhood", entry, hops],
    enabled: !!entry && !!user?.user_id && !checking,
    // Single server-side BFS call (was N per-node /relations round trips).
    // The enabled guard requires a resource and a verified account.
    queryFn: () => fetchNeighbors(vault, entry!, hops, 200),
  });
}
