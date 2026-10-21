// frontend/src/components/graph/graph-types.ts
export type NodeKind = "document" | "table" | "file";

export type RelationKind =
  | "depends_on"
  | "implements"
  | "references"
  | "related_to"
  | "attached_to"
  | "derived_from"
  // Body markdown / wikilink references (`[[…]]`, `[..](..)`) extracted by
  // the backend as implicit edges. Omitting it here silently dropped every
  // body-link edge from the graph even when the backend returned it.
  | "links_to";

export const ALL_NODE_KINDS: ReadonlyArray<NodeKind> = ["document", "table", "file"];
export const ALL_RELATIONS: ReadonlyArray<RelationKind> = [
  "depends_on", "implements", "references", "related_to", "attached_to", "derived_from",
  "links_to",
];

export interface GraphNode {
  uri: string;
  name: string;
  /** The document's own title when the server reports one; labels prefer it over ``name``. */
  title?: string;
  /** When the resource last changed (ISO 8601), when the server reports it. */
  updated_at?: string;
  kind: NodeKind;
  doc_id?: string;
  doc_type?: string;
  /** Server-confirmed global incident-edge count; absent is unknown. */
  degree?: number;
  /** Distance from the requested root in a server neighborhood response. */
  depth?: number;
  /** Cluster id (top-level collection) for grouping/coloring/hulls.
   *  Derived from the URI at render time; null = ungrouped. See cluster.ts. */
  group?: string | null;
  // simulation-owned positional state
  x?: number; y?: number; vx?: number; vy?: number; fx?: number; fy?: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  relation: RelationKind;
  /** Server-reported provenance. Never infer it from the relation label. */
  kind?: "implicit" | "explicit";
  /** Server-reported strength: an explicit relation is strong unless made weak; a body link is always weak. */
  edge_type?: "strong" | "weak";
}

/** A stable key for one edge (endpoints, relation and provenance); endpoints are canonical URI strings. */
export function edgeId(e: Pick<GraphEdge, "source" | "target" | "relation" | "kind">): string {
  return [e.source, e.target, e.relation, e.kind ?? ""].join("\u0000");
}

/** The strength of a connection: the server's ``edge_type``; from a server that does not report it, an explicit
 *  relation is strong and anything else weak. Never inferred from the relation label. */
export function edgeStrength(edge: Pick<GraphEdge, "kind" | "edge_type">): "strong" | "weak" {
  if (edge.edge_type === "strong" || edge.edge_type === "weak") return edge.edge_type;
  return edge.kind === "explicit" ? "strong" : "weak";
}

/** A related-resource reference from the detail panel's RELATIONS list —
 *  carries enough (name, kind, relation, direction, source) to materialize the node
 *  AND its edge in the graph when the relation isn't currently rendered. */
export interface RelatedRef {
  uri: string;
  name: string;
  kind: NodeKind;
  relation: RelationKind;
  direction: "incoming" | "outgoing";
  /** Relation provenance, distinct from the related resource's kind. */
  source?: GraphEdge["kind"];
}

export interface GraphView {
  entry?: string;
  // BFS traversal radius in edge hops from the entry node. The
  // backend's `akb_graph` uses the same word for its server-side
  // BFS — keeping the name aligned across layers signals the
  // single concept (graph traversal distance). Pre-0.3.0 this
  // field was called `depth`, which collided with the also-
  // renamed `akb_browse.depth` (collection-tree depth).
  hops: 1 | 2 | 3;
  /** Around a focus: follow explicit relations only ("strong", the default)
   *  or every loaded connection, body links included ("all"). */
  links: "strong" | "all";
  /** Only resources changed within the last N days ("all" = any time). */
  since: GraphSince;
  types: Set<NodeKind>;
  relations: Set<RelationKind>;
  selected?: string;
}

/** Colour tokens for the distance from the focused resource: the focus, then
 *  1, 2 and 3 hops away. A lightness-ramped run of the categorical scale, so
 *  neighbouring steps differ slightly and the focus stands out in the accent. */
export const HOP_TOKENS = ["cat-5", "cat-1", "cat-2", "cat-3"] as const;

/** What a resource is called on the map: its title, else its name without the collection path and extension. */
export function displayName(node: Pick<GraphNode, "name" | "title">): string {
  const title = node.title?.trim();
  if (title) return title;
  const leaf = node.name.split("/").filter(Boolean).at(-1) ?? node.name;
  return leaf.replace(/\.md$/i, "") || node.name;
}

/** The short category shown before a label: the document type when known, else the collection it lives in. */
export function categoryOf(node: Pick<GraphNode, "doc_type" | "group">): string {
  return node.doc_type?.trim() || node.group?.split("/").at(-1) || "";
}

export type GraphSince = "all" | "1" | "7" | "30";
export const GRAPH_SINCE: GraphSince[] = ["1", "7", "30", "all"];

/** Labels on the map are kept short; the inspector and tooltips carry the full title. */
export const LABEL_MAX_CHARS = 15;
export function shortName(node: Pick<GraphNode, "name" | "title">): string {
  const full = displayName(node), chars = [...full];
  return chars.length > LABEL_MAX_CHARS ? chars.slice(0, LABEL_MAX_CHARS).join("") + "…" : full;
}

export const DEFAULT_VIEW: GraphView = {
  hops: 1,
  links: "strong",
  since: "all",
  types: new Set(ALL_NODE_KINDS),
  relations: new Set(ALL_RELATIONS),
};

export interface GraphColors {
  background: string;
  surface: string;
  surfaceMuted: string;
  foreground: string;
  foregroundMuted: string;
  /** Teal brand (`--color-primary`) — the single SELECTION/focus signal
   *  (selected node border + halo, incident edges, focused-neighbor labels). */
  primary: string;
  /** Orange brand (`--color-accent`) — reserved now for the PINNED marker only,
   *  so selection (teal) never competes with a second orange. */
  accent: string;
  border: string;
  /** Tokenized categorical scale (--color-cat-1..6) for cluster coloring.
   *  getComputedStyle already returns the .dark-overridden value, so this
   *  is the single source of truth for cluster colors in both themes. */
  cat: string[];
}

export function readColors(): GraphColors {
  const root = getComputedStyle(document.documentElement);
  const pick = (name: string) => root.getPropertyValue(name).trim() || "#000";
  return {
    background: pick("--color-background"),
    surface: pick("--color-surface"),
    surfaceMuted: pick("--color-surface-muted"),
    foreground: pick("--color-foreground"),
    foregroundMuted: pick("--color-foreground-muted"),
    primary: pick("--color-primary"),
    accent: pick("--color-accent"),
    border: pick("--color-border"),
    cat: [1, 2, 3, 4, 5, 6].map((i) => root.getPropertyValue(`--color-cat-${i}`).trim() || "gray"),
  };
}

export function kindToSegment(kind: NodeKind): "doc" | "table" | "file" {
  return kind === "table" ? "table" : kind === "file" ? "file" : "doc";
}

export const RELATION_DASH: Record<RelationKind, number[]> = {
  depends_on: [],
  implements: [],
  references: [4, 4],
  related_to: [2, 2],
  attached_to: [],
  derived_from: [6, 2],
  links_to: [1, 3],
};

/** Two-tier edge encoding so the seven typed relations read at a glance:
 *  STRUCTURAL ties (hard dependencies / containment) paint solid, darker, and
 *  thicker; ASSOCIATIVE ties (soft references / body links) paint dashed,
 *  muted, and thinner. Color/weight is the primary channel, the per-relation
 *  dash (RELATION_DASH) the secondary one — together they stay distinguishable
 *  without a seven-color rainbow. `links_to` (implicit body wikilinks) is the
 *  lightest, as the weakest signal. */
export type EdgeClass = "structural" | "associative";
export const RELATION_CLASS: Record<RelationKind, EdgeClass> = {
  depends_on: "structural",
  implements: "structural",
  derived_from: "structural",
  attached_to: "structural",
  references: "associative",
  related_to: "associative",
  links_to: "associative",
};

/** Human-readable relation labels for the legend / chips (no raw snake_case). */
export const RELATION_LABEL: Record<RelationKind, string> = {
  depends_on: "depends on",
  implements: "implements",
  derived_from: "derived from",
  attached_to: "attached to",
  references: "references",
  related_to: "related to",
  links_to: "links to",
};
