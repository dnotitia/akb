export type TableCellValueKind = "json" | "text" | "number" | "boolean" | "null";

export interface TableCellValueText {
  /** The complete value as text: what an expanded table cell shows and copies. */
  text: string;
  kind: TableCellValueKind;
}

/**
 * The full, untruncated text of a table cell value. The grid keeps its own
 * compact single-line rendering; this is the long form for reading and
 * copying: objects and arrays as indented JSON, strings exactly as stored.
 */
export function formatCellValue(value: unknown): TableCellValueText {
  if (value === null || value === undefined) return { text: "null", kind: "null" };
  if (typeof value === "object") return { text: JSON.stringify(value, null, 2), kind: "json" };
  if (typeof value === "number") {
    return { text: Number.isFinite(value) ? String(value) : "—", kind: "number" };
  }
  if (typeof value === "boolean") return { text: String(value), kind: "boolean" };
  return { text: String(value), kind: "text" };
}
