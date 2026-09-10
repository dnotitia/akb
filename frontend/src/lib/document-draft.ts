import { DOC_TYPES, type DocType } from "@/lib/doc-constants";

const DRAFT_VERSION = 2;
const DRAFT_PREFIX = "akb:document-draft:";

/**
 * Existing-document drafts are a separate record from the new-document
 * composer.  A draft is deliberately pinned to the public markdown contract
 * rather than a private Plate schema: package semver + MarkdownProfile are
 * the durable compatibility boundary owned by the shared editor package.
 */
const DOCUMENT_EDIT_DRAFT_VERSION = 2;
const DOCUMENT_EDIT_DRAFT_KIND = "document-edit";
const DOCUMENT_EDIT_DRAFT_PREFIX = "akb:document-edit-draft:";
const DOCUMENT_EDIT_TAB_ID_KEY = "akb:document-edit-tab-id";
export const DOCUMENT_EDIT_DRAFT_EDITOR_VERSION = "0.2.0";
export const DOCUMENT_EDIT_DRAFT_MARKDOWN_PROFILE = "preserve" as const;
export const DOCUMENT_EDIT_DRAFT_RETENTION_HOURS = 24;
const DOCUMENT_EDIT_DRAFT_RETENTION_MS =
  DOCUMENT_EDIT_DRAFT_RETENTION_HOURS * 60 * 60 * 1000;

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
  /** Server-provided expiry per unclaimed attachment, when available. */
  assetExpiresAt?: Record<string, string>;
  updatedAt: string;
  /** Earliest local/attachment recovery boundary. */
  expiresAt?: string;
}

export interface StoredDocumentEditDraft {
  version: typeof DOCUMENT_EDIT_DRAFT_VERSION;
  kind: typeof DOCUMENT_EDIT_DRAFT_KIND;
  draftId: string;
  tabId: string;
  userId: string;
  vault: string;
  document: string;
  baseCommit: string;
  baseTitle: string;
  baseBody: string;
  title: string;
  body: string;
  assetIds: string[];
  /** Server-provided expiry per unclaimed attachment, when available. */
  assetExpiresAt?: Record<string, string>;
  editorVersion: typeof DOCUMENT_EDIT_DRAFT_EDITOR_VERSION;
  markdownProfile: typeof DOCUMENT_EDIT_DRAFT_MARKDOWN_PROFILE;
  updatedAt: string;
  expiresAt: string;
}

export interface IncompatibleDocumentEditDraft {
  userId: string;
  vault: string;
  document: string;
  raw: unknown;
  copyTitle: string;
  copyBody: string;
}

export type DocumentEditDraftLoadResult =
  | { status: "none" }
  | { status: "storage-unavailable" }
  | { status: "restored"; draft: StoredDocumentEditDraft }
  | { status: "expired"; draft: StoredDocumentEditDraft }
  | { status: "incompatible"; draft: IncompatibleDocumentEditDraft };

export type DocumentEditDraftInput = Omit<
  StoredDocumentEditDraft,
  "version" | "kind" | "updatedAt" | "expiresAt"
>;

let fallbackTabId: string | null = null;

function encodeKeyPart(value: string): string {
  return encodeURIComponent(value);
}

function randomId(): string {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch {
    // Hardened browsers can expose crypto without randomUUID. Fall through to
    // the non-secret, collision-resistant enough tab-local fallback.
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function createDocumentEditDraftId(): string {
  return randomId();
}

export function documentEditDraftTabId(): string {
  if (fallbackTabId) return fallbackTabId;
  try {
    const existing = window.sessionStorage.getItem(DOCUMENT_EDIT_TAB_ID_KEY);
    if (existing) {
      fallbackTabId = existing;
      return existing;
    }
    const created = randomId();
    window.sessionStorage.setItem(DOCUMENT_EDIT_TAB_ID_KEY, created);
    fallbackTabId = created;
    return created;
  } catch {
    fallbackTabId = randomId();
    return fallbackTabId;
  }
}

function documentEditDraftPrefix(
  userId: string,
  vault: string,
  document: string,
): string {
  return [
    DOCUMENT_EDIT_DRAFT_PREFIX,
    encodeKeyPart(userId),
    encodeKeyPart(vault),
    encodeKeyPart(document),
  ].join(":");
}

export function documentEditDraftStorageKey(
  userId: string,
  vault: string,
  document: string,
  tabId: string,
  draftId: string,
): string {
  return [
    documentEditDraftPrefix(userId, vault, document),
    encodeKeyPart(tabId),
    encodeKeyPart(draftId),
  ].join(":");
}

function recordLooksLikeEditDraft(value: unknown): value is StoredDocumentEditDraft {
  if (!value || typeof value !== "object") return false;
  const draft = value as Partial<StoredDocumentEditDraft>;
  return (
    draft.version === DOCUMENT_EDIT_DRAFT_VERSION &&
    draft.kind === DOCUMENT_EDIT_DRAFT_KIND &&
    draft.editorVersion === DOCUMENT_EDIT_DRAFT_EDITOR_VERSION &&
    draft.markdownProfile === DOCUMENT_EDIT_DRAFT_MARKDOWN_PROFILE &&
    typeof draft.draftId === "string" &&
    typeof draft.tabId === "string" &&
    typeof draft.userId === "string" &&
    typeof draft.vault === "string" &&
    typeof draft.document === "string" &&
    typeof draft.baseCommit === "string" &&
    typeof draft.baseTitle === "string" &&
    typeof draft.baseBody === "string" &&
    typeof draft.title === "string" &&
    typeof draft.body === "string" &&
    Array.isArray(draft.assetIds) &&
    draft.assetIds.every((assetId) => typeof assetId === "string") &&
    (draft.assetExpiresAt === undefined || (
      !!draft.assetExpiresAt &&
      typeof draft.assetExpiresAt === "object" &&
      !Array.isArray(draft.assetExpiresAt) &&
      Object.entries(draft.assetExpiresAt).every(
        ([assetId, expiresAt]) => typeof assetId === "string" && typeof expiresAt === "string",
      )
    )) &&
    typeof draft.updatedAt === "string" &&
    typeof draft.expiresAt === "string"
  );
}

function copyableIncompatibleDraft(
  raw: unknown,
  userId: string,
  vault: string,
  document: string,
): IncompatibleDocumentEditDraft {
  const candidate = raw && typeof raw === "object"
    ? raw as Record<string, unknown>
    : {};
  const title = typeof candidate.title === "string"
    ? candidate.title
    : typeof candidate.localTitle === "string"
      ? candidate.localTitle
      : "";
  const body = typeof candidate.body === "string"
    ? candidate.body
    : typeof candidate.localBody === "string"
      ? candidate.localBody
      : typeof candidate.content === "string"
        ? candidate.content
        : JSON.stringify(raw, null, 2);
  return { userId, vault, document, raw, copyTitle: title, copyBody: body };
}

function parseStoredEditDraft(
  raw: string,
  userId: string,
  vault: string,
  document: string,
): StoredDocumentEditDraft | IncompatibleDocumentEditDraft | null {
  try {
    const value = JSON.parse(raw) as unknown;
    if (recordLooksLikeEditDraft(value)) {
      if (
        value.userId !== userId ||
        value.vault !== vault ||
        value.document !== document ||
        Number.isNaN(Date.parse(value.updatedAt)) ||
        Number.isNaN(Date.parse(value.expiresAt))
      ) {
        return null;
      }
      return value;
    }
    return copyableIncompatibleDraft(value, userId, vault, document);
  } catch {
    return copyableIncompatibleDraft(raw, userId, vault, document);
  }
}

function readDocumentEditDraftEntries(
  userId: string,
  vault: string,
  document: string,
): { active: StoredDocumentEditDraft[]; incompatible: IncompatibleDocumentEditDraft[] } {
  const active: StoredDocumentEditDraft[] = [];
  const incompatible: IncompatibleDocumentEditDraft[] = [];
  const prefix = `${documentEditDraftPrefix(userId, vault, document)}:`;
  for (let index = 0; index < window.localStorage.length; index += 1) {
    const key = window.localStorage.key(index);
    if (!key?.startsWith(prefix)) continue;
    const raw = window.localStorage.getItem(key);
    if (raw === null) continue;
    const parsed = parseStoredEditDraft(raw, userId, vault, document);
    if (!parsed) continue;
    if (recordLooksLikeEditDraft(parsed)) active.push(parsed);
    else incompatible.push(parsed);
  }
  active.sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt));
  return { active, incompatible };
}

export function loadDocumentEditDraft(
  userId: string,
  vault: string,
  document: string,
  tabId = documentEditDraftTabId(),
  now = new Date(),
): DocumentEditDraftLoadResult {
  try {
    const entries = readDocumentEditDraftEntries(userId, vault, document);
    const exactTab = entries.active.filter((draft) => draft.tabId === tabId);
    const candidate = [...exactTab, ...entries.active.filter((draft) => draft.tabId !== tabId)][0];
    if (candidate) {
      return Date.parse(candidate.expiresAt) <= now.getTime()
        ? { status: "expired", draft: candidate }
        : { status: "restored", draft: candidate };
    }
    const incompatible = entries.incompatible[0];
    return incompatible
      ? { status: "incompatible", draft: incompatible }
      : { status: "none" };
  } catch {
    return { status: "storage-unavailable" };
  }
}

export function listDocumentEditDrafts(
  userId: string,
  vault: string,
  document: string,
): StoredDocumentEditDraft[] {
  try {
    return readDocumentEditDraftEntries(userId, vault, document).active;
  } catch {
    return [];
  }
}

export function saveDocumentEditDraft(draft: DocumentEditDraftInput): boolean {
  const updatedAt = new Date();
  const expiresAt = new Date(
    updatedAt.getTime() + DOCUMENT_EDIT_DRAFT_RETENTION_MS,
  );
  const assetExpiresAt = Object.fromEntries(
    Object.entries(draft.assetExpiresAt ?? {}).filter(
      ([assetId, value]) =>
        draft.assetIds.includes(assetId) && !Number.isNaN(Date.parse(value)),
    ),
  );
  for (const value of Object.values(assetExpiresAt)) {
    if (Date.parse(value) < expiresAt.getTime()) expiresAt.setTime(Date.parse(value));
  }
  const stored: StoredDocumentEditDraft = {
    ...draft,
    assetExpiresAt,
    version: DOCUMENT_EDIT_DRAFT_VERSION,
    kind: DOCUMENT_EDIT_DRAFT_KIND,
    updatedAt: updatedAt.toISOString(),
    expiresAt: expiresAt.toISOString(),
  };
  try {
    window.localStorage.setItem(
      documentEditDraftStorageKey(
        stored.userId,
        stored.vault,
        stored.document,
        stored.tabId,
        stored.draftId,
      ),
      JSON.stringify(stored),
    );
    return true;
  } catch {
    return false;
  }
}

export function clearDocumentEditDraft(draft: Pick<StoredDocumentEditDraft, "userId" | "vault" | "document" | "tabId" | "draftId">): void {
  try {
    window.localStorage.removeItem(
      documentEditDraftStorageKey(
        draft.userId,
        draft.vault,
        draft.document,
        draft.tabId,
        draft.draftId,
      ),
    );
  } catch {
    // A blocked storage area cannot be cleared reliably. The record remains
    // isolated and the server TTL is still the attachment safety net.
  }
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
    const updatedAt = new Date();
    const assetExpiresAt = Object.fromEntries(
      Object.entries(draft.assetExpiresAt ?? {}).filter(
        ([assetId, value]) =>
          draft.assetIds.includes(assetId) && !Number.isNaN(Date.parse(value)),
      ),
    );
    const expiresAt = new Date(
      updatedAt.getTime() + DOCUMENT_EDIT_DRAFT_RETENTION_MS,
    );
    for (const value of Object.values(assetExpiresAt)) {
      if (Date.parse(value) < expiresAt.getTime()) expiresAt.setTime(Date.parse(value));
    }
    window.localStorage.setItem(
      documentDraftStorageKey(draft.vault),
      JSON.stringify({
        ...draft,
        assetExpiresAt,
        version: DRAFT_VERSION,
        updatedAt: updatedAt.toISOString(),
        expiresAt: expiresAt.toISOString(),
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
