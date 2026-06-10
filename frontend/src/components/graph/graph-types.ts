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
  accent: string;
  border: string;
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
    accent: pick("--color-accent"),
    border: pick("--color-border"),
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
