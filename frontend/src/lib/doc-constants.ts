export const DOC_TYPES = [
  "note",
  "report",
  "decision",
  "spec",
  "plan",
  "session",
  "task",
  "reference",
] as const;
export type DocType = (typeof DOC_TYPES)[number];

export const DOC_STATUSES = ["draft", "active", "archived"] as const;
export type DocStatus = (typeof DOC_STATUSES)[number];
