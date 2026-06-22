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
  kind: NodeKind;
  doc_id?: string;
  doc_type?: string;
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
}

/** A related-resource reference from the detail panel's RELATIONS list —
 *  carries enough (name, kind, relation, direction) to materialize the node
 *  AND its edge in the graph when the relation isn't currently rendered. */
export interface RelatedRef {
  uri: string;
  name: string;
  kind: NodeKind;
  relation: RelationKind;
  direction: "incoming" | "outgoing";
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
  types: Set<NodeKind>;
  relations: Set<RelationKind>;
  selected?: string;
}

export const DEFAULT_VIEW: GraphView = {
  hops: 2,
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
