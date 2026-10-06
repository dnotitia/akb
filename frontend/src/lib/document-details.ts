import type { DocumentUpdateInput } from "@/lib/api";

export interface DocumentDetailsValues {
  summary: string;
  type: string;
  domain: string;
  tags: string[];
  status: string;
}

type DocumentDetailsSource = {
  [K in keyof DocumentDetailsValues]?: DocumentDetailsValues[K] | null;
};

type DocumentDetailsUpdate = Pick<
  DocumentUpdateInput,
  "summary" | "type" | "domain" | "tags" | "status"
>;

/** Preserve server values, including types/statuses this client does not know. */
export function documentDetailsFrom(document: DocumentDetailsSource): DocumentDetailsValues {
  return {
    summary: document.summary ?? "",
    type: document.type ?? "",
    domain: document.domain ?? "",
    tags: [...(document.tags ?? [])],
    status: document.status ?? "",
  };
}

export function isDocumentDetailsValues(value: unknown): value is DocumentDetailsValues {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const details = value as Partial<DocumentDetailsValues>;
  return (
    typeof details.summary === "string" &&
    typeof details.type === "string" &&
    typeof details.domain === "string" &&
    Array.isArray(details.tags) &&
    details.tags.every((tag) => typeof tag === "string") &&
    typeof details.status === "string"
  );
}

function serializedText(value: string): string {
  // The backend's NFCModel normalizes all incoming strings and removes NUL.
  return value.normalize("NFC").replaceAll("\0", "");
}

function nullableText(value: string): string {
  return serializedText(value).trim();
}

function sameTags(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((tag, index) => serializedText(tag) === serializedText(b[index]));
}

/** Match nullable text serialization without modifying the hydrated snapshots. */
export function documentDetailsEqual(a: DocumentDetailsValues, b: DocumentDetailsValues): boolean {
  return (
    nullableText(a.summary) === nullableText(b.summary) &&
    serializedText(a.type) === serializedText(b.type) &&
    nullableText(a.domain) === nullableText(b.domain) &&
    sameTags(a.tags, b.tags) &&
    serializedText(a.status) === serializedText(b.status)
  );
}

/** Only explicit detail changes enter the PATCH; title/body remain separate. */
export function changedDocumentDetails(
  current: DocumentDetailsValues,
  base: DocumentDetailsValues,
): DocumentDetailsUpdate {
  const changed: DocumentDetailsUpdate = {};
  if (nullableText(current.summary) !== nullableText(base.summary)) changed.summary = current.summary.trim() || null;
  if (serializedText(current.type) !== serializedText(base.type)) changed.type = current.type;
  if (nullableText(current.domain) !== nullableText(base.domain)) changed.domain = current.domain.trim() || null;
  if (!sameTags(current.tags, base.tags)) changed.tags = [...current.tags];
  // Unknown existing statuses are retained in the snapshots and omitted from
  // updates. An explicit selection is the only path that writes a new status.
  if (serializedText(current.status) !== serializedText(base.status)) changed.status = current.status as DocumentUpdateInput["status"];
  return changed;
}
