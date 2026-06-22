// frontend/src/components/graph/graph-swatches.tsx
//
// Shared DOM swatches for the graph's visual vocabulary, so the on-canvas
// legend (GraphCanvas) and the sidebar's legend-as-control (GraphSidebar) read
// from ONE source instead of re-inlining the kind silhouettes + edge encoding
// (which must stay in lockstep with traceNode / paintLink on the canvas).
import { cn } from "@/lib/utils";
import {
  RELATION_CLASS,
  RELATION_DASH,
  type NodeKind,
  type RelationKind,
} from "./graph-types";

/** The kind's canvas silhouette as a small DOM swatch — document = circle,
 *  table = rounded square, file = dashed-ring circle. Mirrors `traceNode`. */
export function KindSwatch({ kind }: { kind: NodeKind }) {
  const base = "inline-block h-3 w-3 shrink-0";
  if (kind === "table")
    return <span aria-hidden className={cn(base, "border border-foreground rounded-[var(--radius-xs)] bg-surface")} />;
  if (kind === "file")
    return <span aria-hidden className={cn(base, "border border-dashed border-foreground-muted rounded-full")} />;
  return <span aria-hidden className={cn(base, "border border-foreground rounded-full bg-surface-muted")} />;
}

/** A short line in the relation's own canvas encoding — structural ties read
 *  darker + thicker, associative muted + thinner, with each relation's dash —
 *  so the legend/sidebar swatch matches the edge `paintLink` draws. */
export function RelationSwatch({ relation }: { relation: RelationKind }) {
  const structural = RELATION_CLASS[relation] === "structural";
  const dash = RELATION_DASH[relation].join(" ") || undefined;
  return (
    <svg width="20" height="6" aria-hidden className={structural ? "text-foreground" : "text-foreground-muted"}>
      <line
        x1="0"
        y1="3"
        x2="20"
        y2="3"
        stroke="currentColor"
        strokeWidth={structural ? 1.6 : 1.1}
        strokeDasharray={dash}
      />
    </svg>
  );
}
