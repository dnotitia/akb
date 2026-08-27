import { DOC_TYPES, type DocType } from "@/lib/doc-constants";

const DRAFT_VERSION = 1;
const DRAFT_PREFIX = "akb:document-draft:";

export interface StoredDocumentDraft {
  version: typeof DRAFT_VERSION;
  vault: string;
  title: string;
  collection: string;
  type: DocType;
  domain: string;
  summary: string;
  tags: string[];
  body: string;
  assetIds: string[];
  updatedAt: string;
}

export function documentDraftStorageKey(vault: string): string {
  return `${DRAFT_PREFIX}${encodeURIComponent(vault)}`;
}

export function loadDocumentDraft(vault: string): StoredDocumentDraft | null {
  try {
    const raw = window.localStorage.getItem(documentDraftStorageKey(vault));
    if (!raw) return null;
    const draft = JSON.parse(raw) as Partial<StoredDocumentDraft>;
    if (
      draft.version !== DRAFT_VERSION ||
      draft.vault !== vault ||
      typeof draft.title !== "string" ||
      typeof draft.collection !== "string" ||
      !DOC_TYPES.includes(draft.type as DocType) ||
      typeof draft.domain !== "string" ||
      typeof draft.summary !== "string" ||
      !Array.isArray(draft.tags) ||
      !draft.tags.every((tag) => typeof tag === "string") ||
      typeof draft.body !== "string" ||
      !Array.isArray(draft.assetIds) ||
      !draft.assetIds.every((assetId) => typeof assetId === "string") ||
      typeof draft.updatedAt !== "string"
    ) {
      window.localStorage.removeItem(documentDraftStorageKey(vault));
      return null;
    }
    return draft as StoredDocumentDraft;
  } catch {
    return null;
  }
}

export function saveDocumentDraft(
  draft: Omit<StoredDocumentDraft, "version" | "updatedAt">,
): boolean {
  try {
    window.localStorage.setItem(
      documentDraftStorageKey(draft.vault),
      JSON.stringify({
        ...draft,
        version: DRAFT_VERSION,
        updatedAt: new Date().toISOString(),
      } satisfies StoredDocumentDraft),
    );
    return true;
  } catch {
    return false;
  }
}

export function clearDocumentDraft(vault: string): void {
  try {
    window.localStorage.removeItem(documentDraftStorageKey(vault));
  } catch {
    // Storage can be unavailable in hardened/private browsing contexts. The
    // in-memory composer remains usable and its close confirmation still
    // protects the current session.
  }
}
