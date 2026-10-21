// frontend/src/components/graph/graph-swatches.tsx
//
// GraphNodeMarker matches the canvas; KindSwatch uses the app's familiar glyphs.
// Relations remain neutral; their meaning is
// communicated by text, while canvas dashes encode source, not relation kind.
import { FileText, Table2, Paperclip } from "lucide-react";
import {
  HOP_TOKENS,
  type NodeKind,
  type RelationKind,
} from "./graph-types";

/** Flat canvas symbols paired with text in the interactive legend. ``neutral``
 *  drops the kind colour while the canvas colours resources by hop distance. */
export function GraphNodeMarker({ kind, neutral = false }: { kind: NodeKind; neutral?: boolean }) {
  const tone = neutral ? "text-foreground-muted" : kind === "table" ? "text-cat-3" : kind === "file" ? "text-cat-4" : "text-cat-1";
  return <svg viewBox="0 0 16 16" aria-hidden className={`h-3.5 w-3.5 shrink-0 ${tone}`}>
    {kind === "table" ? <rect x="3.5" y="3.5" width="9" height="9" fill="currentColor" />
      : kind === "file" ? <path d="M8 1.5 14.5 8 8 14.5 1.5 8Z" fill="currentColor" />
      : <circle cx="8" cy="8" r="5" fill="currentColor" />}
  </svg>;
}

/** The colour of each hop distance on the canvas, with its label. */
export function HopLegend({ hops }: { hops: 1 | 2 | 3 }) {
  return <span className="flex items-center gap-2" aria-label="Hop colours">
    {HOP_TOKENS.slice(0, hops + 1).map((token, hop) => <span key={token} className="flex items-center gap-1 text-foreground-muted">
      <svg viewBox="0 0 16 16" aria-hidden className="h-3 w-3 shrink-0" style={{ color: `var(--color-${token})` }}><circle cx="8" cy="8" r="5" fill="currentColor" /></svg>
      {hop === 0 ? "Focus" : hop}
    </span>)}
  </span>;
}

/** Kind remains distinguishable without colour. */
export function KindSwatch({ kind }: { kind: NodeKind }) {
  const base = "h-3.5 w-3.5 shrink-0";
  if (kind === "table")
    return <Table2 aria-hidden className={`${base} text-cat-3`} />;
  if (kind === "file")
    return <Paperclip aria-hidden className={`${base} text-cat-4`} />;
  return <FileText aria-hidden className={`${base} text-cat-1`} />;
}

export function RelationSwatch(_props: { relation: RelationKind }) {
  return (
    <svg width="20" height="6" aria-hidden className="text-foreground-muted">
      <line
        x1="0"
        y1="3"
        x2="20"
        y2="3"
        stroke="currentColor"
        strokeWidth={1.1}
      />
    </svg>
  );
}
