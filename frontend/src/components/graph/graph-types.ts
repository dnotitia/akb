// frontend/src/components/graph/graph-types.ts
export type NodeKind = "document" | "table" | "file";

export type RelationKind =
  | "depends_on"
  | "implements"
  | "references"
  | "related_to"
  | "attached_to"
  | "derived_from";

export const ALL_NODE_KINDS: ReadonlyArray<NodeKind> = ["document", "table", "file"];
export const ALL_RELATIONS: ReadonlyArray<RelationKind> = [
  "depends_on", "implements", "references", "related_to", "attached_to", "derived_from",
];

export interface GraphNode {
  uri: string;
  name: string;
  kind: NodeKind;
  doc_id?: string;
  doc_type?: string;
  // simulation-owned positional state
  x?: number; y?: number; vx?: number; vy?: number; fx?: number; fy?: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  relation: RelationKind;
}

export interface GraphView {
  entry?: string;
  depth: 1 | 2 | 3;
  types: Set<NodeKind>;
  relations: Set<RelationKind>;
  selected?: string;
}

export const DEFAULT_VIEW: GraphView = {
  depth: 2,
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

export const RELATION_DASH: Record<RelationKind, number[]> = {
  depends_on: [],
  implements: [],
  references: [4, 4],
  related_to: [2, 2],
  attached_to: [],
  derived_from: [6, 2],
};
