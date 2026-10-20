import { documentDetailsEqual, documentDetailsFrom, type DocumentDetailsValues } from "@/lib/document-details";
import { documentTitleKey } from "@/lib/document-title-conflict";

export interface DocumentConflictSnapshot {
  label: string;
  commit: string | null;
  title: string;
  body: string;
  details?: DocumentDetailsValues;
}

export interface DocumentConflictResult {
  title: string;
  body: string;
  details: DocumentDetailsValues;
}

export type DocumentConflictField = "title" | "body" | keyof DocumentDetailsValues;

/** Owned by the editor comparison so transient view remounts cannot lose work. */
export interface DocumentConflictResolution {
  result: DocumentConflictResult;
  reviewed: DocumentConflictField[];
}

/** Merge independent fields, never infer a textual or semantic body merge. */
export function planDocumentConflict(
  base: DocumentConflictSnapshot,
  local: DocumentConflictSnapshot,
  latest: DocumentConflictSnapshot,
): { result: DocumentConflictResult; conflicts: DocumentConflictField[] } {
  const conflicts: DocumentConflictField[] = [];
  function choose<T>(field: DocumentConflictField, before: T, mine: T, current: T, equal: (a: T, b: T) => boolean): T {
    if (equal(mine, before)) return current;
    if (equal(current, before) || equal(mine, current)) return mine;
    conflicts.push(field);
    return current;
  }
  const sameText = (a: string, b: string) => a === b;
  const sameTitle = (a: string, b: string) => documentTitleKey(a) === documentTitleKey(b);
  const baseDetails = documentDetailsFrom(base.details ?? {});
  const localDetails = documentDetailsFrom(local.details ?? {});
  const latestDetails = documentDetailsFrom(latest.details ?? {});
  const details = { ...latestDetails };
  for (const field of Object.keys(details) as (keyof DocumentDetailsValues)[]) {
    const selected = choose(field, baseDetails[field], localDetails[field], latestDetails[field],
      (a, b) => documentDetailsEqual({ ...baseDetails, [field]: a }, { ...baseDetails, [field]: b }));
    Object.assign(details, { [field]: selected });
  }
  return {
    result: {
      title: choose("title", base.title, local.title, latest.title, sameTitle),
      body: choose("body", base.body, local.body, latest.body, sameText),
      details,
    },
    conflicts,
  };
}
