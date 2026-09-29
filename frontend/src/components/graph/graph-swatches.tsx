// frontend/src/components/graph/graph-swatches.tsx
//
// GraphNodeMarker matches the canvas; KindSwatch uses the app's familiar glyphs.
// Relations remain neutral; their meaning is
// communicated by text, while canvas dashes encode source, not relation kind.
import { FileText, Table2, Paperclip } from "lucide-react";
import {
  type NodeKind,
  type RelationKind,
} from "./graph-types";

/** Flat canvas symbols paired with text in the interactive legend. */
export function GraphNodeMarker({ kind }: { kind: NodeKind }) {
  return <svg viewBox="0 0 16 16" aria-hidden className={`h-3.5 w-3.5 shrink-0 ${kind === "table" ? "text-cat-3" : kind === "file" ? "text-cat-4" : "text-cat-1"}`}>
    {kind === "table" ? <rect x="3.5" y="3.5" width="9" height="9" fill="currentColor" />
      : kind === "file" ? <path d="M8 1.5 14.5 8 8 14.5 1.5 8Z" fill="currentColor" />
      : <circle cx="8" cy="8" r="5" fill="currentColor" />}
  </svg>;
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
