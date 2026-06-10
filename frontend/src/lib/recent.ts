import { File as FileIcon, FileText, Table as TableIcon } from "lucide-react";

// Shared recent-activity row vocabulary, used by both the Home dashboard and the
// per-vault overview so a recent change reads identically wherever it appears.

// Leading icon for a recent change, by resource kind. Tables/files use their
// own glyphs; everything else (notes, specs, decisions, …) reads as a document.
export function recentIcon(type?: string) {
  if (type === "table" || type === "table_query") return TableIcon;
  if (type === "file") return FileIcon;
  return FileText;
}

// Leading-glyph tint by kind, from the categorical ramp — a pre-attentive
// "what kind" cue so a doc vs a table vs a file is a glance, not a path-read.
// All doc-ish types collapse to one hue so a list stays ~3 colors (never a
// rainbow); the glyph still carries the real distinction. Deliberately skips
// cat-5 (orange-red) so the type tint never competes with the fresh-token
// spark, which owns the only warm accent a row may show.
export function recentTone(type?: string): string {
  if (type === "table" || type === "table_query") return "var(--color-cat-3)";
  if (type === "file") return "var(--color-cat-4)";
  return "var(--color-cat-1)";
}
