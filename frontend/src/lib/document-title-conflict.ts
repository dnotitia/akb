import { ApiError } from "@/lib/api";

export type DocumentTitleConflict = {
  title: string;
  collection: string;
  existingPath: string;
  existingTitle: string;
  exactContent?: boolean;
};

export type DocumentTitleCandidate = {
  name: string;
  path: string;
};

export function documentTitleKey(title: string) {
  return title.normalize("NFC").trim();
}

export function documentCollection(path: string) {
  const separator = path.lastIndexOf("/");
  return separator < 0 ? "" : path.slice(0, separator);
}

export function findDocumentTitleConflict(
  documents: readonly DocumentTitleCandidate[],
  title: string,
  collection: string,
  excludePath?: string,
): DocumentTitleConflict | null {
  const titleKey = documentTitleKey(title);
  if (!titleKey) return null;
  const existing = documents.find(
    (document) =>
      document.path !== excludePath &&
      documentCollection(document.path) === collection &&
      documentTitleKey(document.name) === titleKey,
  );
  return existing
    ? {
        title: titleKey,
        collection,
        existingPath: existing.path,
        existingTitle: existing.name,
      }
    : null;
}

export function documentTitleConflictFromError(
  error: unknown,
): DocumentTitleConflict | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = error.detail as {
    code?: unknown;
    details?: {
      title?: unknown;
      collection?: unknown;
      existing_path?: unknown;
      existing_title?: unknown;
    };
  };
  const fields = detail?.details;
  if (
    detail?.code !== "document_title_conflict" ||
    !fields ||
    typeof fields.title !== "string" ||
    typeof fields.collection !== "string" ||
    typeof fields.existing_path !== "string" ||
    typeof fields.existing_title !== "string"
  ) {
    return null;
  }
  return {
    title: fields.title,
    collection: fields.collection,
    existingPath: fields.existing_path,
    existingTitle: fields.existing_title,
  };
}

export function titleConflictLocation(collection: string) {
  return collection || "Vault root";
}
