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
  edgeId,
  edgeStrength,
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

/** ``{ [key]: value }`` for a non-blank string from a server that may not send the field, else nothing. */
function optionalText<K extends string>(key: K, value: unknown): Partial<Record<K, string>> {
  return typeof value === "string" && value.trim() ? { [key]: value } as Record<K, string> : {};
}

/** Convert a backend /graph response into the renderer's payload shape.
 *  Shared by the full-graph load and on-demand node expansion so both map
 *  kinds/relations/groups identically. */
export function apiToPayload(resp: GraphOverviewResponse): GraphPayload {
  const nodes: GraphNode[] = resp.nodes.map((n) => ({
    uri: n.uri,
    name: n.name || n.uri,
    ...optionalText("title", (n as unknown as { title?: unknown }).title),
    ...optionalText("doc_type", (n as unknown as { doc_type?: unknown }).doc_type),
    ...optionalText("updated_at", (n as unknown as { updated_at?: unknown }).updated_at),
    kind: normalizeKind(n.resource_type),
    group: groupOf(n.uri),
    degree: n.degree ?? undefined,
    depth: n.depth ?? undefined,
  }));
  const edges: GraphEdge[] = resp.edges
    .map((e): GraphEdge | null => {
      const rel = normalizeRelation(e.relation);
      const edgeType = (e as { edge_type?: unknown }).edge_type;
      return rel ? {
        source: endpointUri(e.source), target: endpointUri(e.target), relation: rel, kind: e.kind,
        ...(edgeType === "strong" || edgeType === "weak" ? { edge_type: edgeType } : {}),
      } : null;
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
/** A focused neighbourhood that keeps only strong connections.
 *
 *  Strong connections are explicit relations: from the focus they are followed
 *  outward (a resource's own context, e.g. an incident's cluster, node, service
 *  and owner) and, for ``derived_from``, also back to what was derived from it
 *  (an incident's postmortem). Following them outward only is what stops a walk
 *  at shared context such as a cluster, instead of fanning out to every other
 *  incident of that cluster. Body links (implicit) are weak: only the focus's
 *  own weak neighbours stay, listed in ``weak`` and never walked further.
 *  A map without any explicit relation is returned unchanged with
 *  ``strong: false``, so a Vault that never declares relations keeps its links. */
export function strongNeighborhood(
  p: GraphPayload,
  focus: string | undefined,
  hops: number,
  /** Resources the reader asked to see (loaded connections, a followed relationship, the selection): always kept. */
  revealed: Iterable<string> = [],
): GraphPayload & StrongScene {
  const explicit = p.edges.filter(e => edgeStrength(e) === "strong" && e.source !== e.target);
  if (!focus || explicit.length === 0) return { ...p, weak: new Set(), weakDepth: new Map(), strong: false, reach: new Map(), tree: new Set(), perHop: [] };
  const reach = new Map<string, number>([[focus, 0]]);
  // The edge by which the walk first reached each resource: the backbone the map draws at full weight.
  const tree = new Set<string>();
  const queue = [focus];
  for (let index = 0; index < queue.length; index += 1) {
    const here = queue[index], next = reach.get(here)! + 1;
    if (next > hops) continue;
    for (const e of explicit) {
      const other = e.source === here ? e.target : e.relation === "derived_from" && e.target === here ? e.source : undefined;
      if (other === undefined || reach.has(other)) continue;
      reach.set(other, next); queue.push(other); tree.add(edgeId(e));
    }
  }
  // How many resources each step adds, for an honest hop control (the walk may end before the chosen depth).
  const perHop = Array.from({ length: hops }, (_, i) => [...reach.values()].filter(d => d === i + 1).length);
  // Weak neighbours are each strongly reached resource's own: within the chosen depth, a resource's body links show
  // one step further out (faded), and are never walked.
  const weakDepth = new Map<string, number>();
  for (const e of p.edges) {
    if (edgeStrength(e) === "strong") continue;
    for (const [from, to] of [[e.source, e.target], [e.target, e.source]] as const) {
      const depth = reach.get(from);
      if (depth === undefined || depth >= hops || reach.has(to)) continue;
      weakDepth.set(to, Math.min(weakDepth.get(to) ?? Infinity, depth + 1));
    }
  }
  const weak = new Set(weakDepth.keys());
  const extra = new Set([...revealed].filter(uri => !reach.has(uri) && !weak.has(uri)));
  const keep = new Set([...reach.keys(), ...weak, ...extra]);
  const edges = p.edges.filter(e => {
    if (!keep.has(e.source) || !keep.has(e.target)) return false;
    if (extra.has(e.source) || extra.has(e.target)) return true;
    if (edgeStrength(e) === "strong") return reach.has(e.source) && reach.has(e.target);
    const near = (a: string, b: string) => (reach.get(a) ?? Infinity) < hops && weak.has(b);
    return near(e.source, e.target) || near(e.target, e.source);
  });
  return { nodes: p.nodes.filter(n => keep.has(n.uri)), edges, meta: p.meta, weak, weakDepth, strong: true, reach, tree, perHop };
}

/** Only resources changed within the last ``since`` days; ``keep`` (the focus, what the reader selected or revealed)
 *  and resources whose time the server does not report always stay. Applied before the walk, so it never passes
 *  through a resource outside the window. */
export function changedWithin(p: GraphPayload, since: GraphView["since"], keep: Iterable<string>, now = Date.now()): GraphPayload {
  if (since === "all") return p;
  const cutoff = now - Number(since) * 24 * 60 * 60 * 1000, kept = new Set(keep);
  const nodes = p.nodes.filter(n => {
    if (kept.has(n.uri) || !n.updated_at) return true;
    const at = Date.parse(n.updated_at);
    return Number.isNaN(at) || at >= cutoff;
  });
  const ids = new Set(nodes.map(n => n.uri));
  return { ...p, nodes, edges: p.edges.filter(e => ids.has(e.source) && ids.has(e.target)) };
}

export interface StrongScene {
  /** Resources joined to a strongly reached resource by a weak connection only (never walked). */
  weak: Set<string>;
  /** Hop at which each weak resource shows: one past the resource whose body link it is. */
  weakDepth: Map<string, number>;
  /** False when the map has no strong connection, so everything loaded is shown. */
  strong: boolean;
  /** Hop distance of every strongly reached resource (the focus is 0). */
  reach: Map<string, number>;
  /** ``edgeId`` of the edge that first reached each resource. */
  tree: Set<string>;
  /** Resources added by hop 1, 2, … up to the chosen depth. */
  perHop: number[];
}


/** Hops from ``start`` over the loaded relationships, either direction. Resources the walk does not reach are absent. */
export function hopDistances(edges: GraphEdge[], start: string | undefined): Map<string, number> {
  const distance = new Map<string, number>();
  if (!start) return distance;
  const neighbors = new Map<string, string[]>();
  for (const { source, target } of edges) {
    if (source === target) continue;
    neighbors.set(source, [...(neighbors.get(source) ?? []), target]);
    neighbors.set(target, [...(neighbors.get(target) ?? []), source]);
  }
  distance.set(start, 0);
  const queue = [start];
  for (let index = 0; index < queue.length; index += 1) {
    const here = queue[index], next = distance.get(here)! + 1;
    for (const other of neighbors.get(here) ?? []) {
      if (distance.has(other)) continue;
      distance.set(other, next); queue.push(other);
    }
  }
  return distance;
}

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
  return { nodes, edges: strongerOnly(edges), meta: p.meta };
}

/** One connection per pair of resources: where a strong connection joins two resources, weak ones between the same
 *  two (in either direction) are dropped, so a relation and the body link that repeats it draw one line. */
export function strongerOnly(edges: GraphEdge[]): GraphEdge[] {
  const pair = (e: GraphEdge) => [e.source, e.target].sort().join("\u0000");
  const strong = new Set(edges.filter(e => edgeStrength(e) === "strong").map(pair));
  return edges.filter(e => edgeStrength(e) === "strong" || !strong.has(pair(e)));
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

/** Strong links around a focus: the server walks strong edges only, so its node budget goes to the strong
 *  neighbourhood, and one hop of every edge adds the focus's own weak neighbours. */
export async function fetchStrongNeighbors(vault: string, id: string, hops: 1 | 2 | 3, limit = 200): Promise<GraphPayload> {
  const strong = await getGraph(vault, id, hops, limit, "strong");
  // Every strongly reached resource short of the chosen depth brings its own weak links: one hop of every edge around
  // it (the focus first; bounded so a large strong neighbourhood stays a handful of requests).
  const around = [...new Map(strong.nodes.map(n => [n.uri, n.depth ?? 0])).entries()]
    .filter(([, depth]) => depth < hops).sort((a, b) => a[1] - b[1]).slice(0, WEAK_SOURCES).map(([uri]) => uri);
  if (!around.includes(id)) around.unshift(id);
  const rings = await Promise.all(around.map(uri => getGraph(vault, uri, 1, 150)));
  const merged = rings.reduce((all, ring) => mergeGraph(all, apiToPayload(ring)), apiToPayload(strong));
  const truncated = (strong as { truncated?: unknown }).truncated === true;
  return truncated ? { ...merged, meta: { ...merged.meta, truncated: true } } : merged;
}

/** How many strongly reached resources contribute their weak links to one neighbourhood. */
const WEAK_SOURCES = 12;

export function useNeighborhood(
  vault: string,
  entry: string | undefined,
  hops: 1 | 2 | 3,
  links: "strong" | "all" = "all",
) {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return useQuery({
    queryKey: ["graph", user?.user_id, revision, vault, "neighborhood", entry, hops, links],
    enabled: !!entry && !!user?.user_id && !checking,
    // Single server-side BFS call (was N per-node /relations round trips).
    // The enabled guard requires a resource and a verified account.
    queryFn: () => links === "strong" ? fetchStrongNeighbors(vault, entry!, hops) : fetchNeighbors(vault, entry!, hops, 200),
  });
}
