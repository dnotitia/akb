import { DOC_TYPES, type DocType } from "@/lib/doc-constants";
import { parseDocUri } from "@/lib/uri";

const DRAFT_VERSION = 1;
const DRAFT_PREFIX = "akb:document-draft:";
export const WORKSPACE_DRAFTS_CHANGED_EVENT = "akb:workspace-drafts-changed";
export const WORKSPACE_DRAFTS_EVENT = WORKSPACE_DRAFTS_CHANGED_EVENT;

function notifyDraftsChanged() {
  window.dispatchEvent(new Event(WORKSPACE_DRAFTS_CHANGED_EVENT));
}

/**
 * Existing-document drafts are a separate record from the new-document
 * composer.  A draft is deliberately pinned to the public markdown contract
 * rather than a private Plate schema: package semver + MarkdownProfile are
 * the durable compatibility boundary owned by the shared editor package.
 */
const DOCUMENT_EDIT_DRAFT_VERSION = 1;
const DOCUMENT_EDIT_DRAFT_KIND = "document-edit";
const DOCUMENT_EDIT_DRAFT_PREFIX = "akb:document-edit-draft:";
const DOCUMENT_EDIT_TAB_ID_KEY = "akb:document-edit-tab-id";
export const DOCUMENT_EDIT_DRAFT_EDITOR_VERSION = "0.1.0";
export const DOCUMENT_EDIT_DRAFT_MARKDOWN_PROFILE = "preserve" as const;
export const DOCUMENT_EDIT_DRAFT_RETENTION_HOURS = 24;
const DOCUMENT_EDIT_DRAFT_RETENTION_MS =
  DOCUMENT_EDIT_DRAFT_RETENTION_HOURS * 60 * 60 * 1000;

export interface StoredDocumentDraft {
  version: typeof DRAFT_VERSION;
  userId: string;
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
  const stored: StoredDocumentEditDraft = {
    ...draft,
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
    notifyDraftsChanged();
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
    notifyDraftsChanged();
  } catch {
    // A blocked storage area cannot be cleared reliably. The record remains
    // isolated and the server TTL is still the attachment safety net.
  }
}

export function documentDraftStorageKey(userId: string, vault: string): string {
  return `${DRAFT_PREFIX}account:${encodeKeyPart(userId)}:${encodeKeyPart(vault)}`;
}

export function loadDocumentDraft(userId: string, vault: string): StoredDocumentDraft | null {
  if (!userId) return null;
  try {
    const raw = window.localStorage.getItem(documentDraftStorageKey(userId, vault));
    if (!raw) return null;
    const draft = JSON.parse(raw) as Partial<StoredDocumentDraft>;
    if (
      draft.version !== DRAFT_VERSION ||
      draft.userId !== userId ||
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
      typeof draft.updatedAt !== "string" ||
      !Number.isFinite(Date.parse(draft.updatedAt))
    ) {
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
  if (!draft.userId) return false;
  try {
    window.localStorage.setItem(
      documentDraftStorageKey(draft.userId, draft.vault),
      JSON.stringify({
        ...draft,
        version: DRAFT_VERSION,
        updatedAt: new Date().toISOString(),
      } satisfies StoredDocumentDraft),
    );
    notifyDraftsChanged();
    return true;
  } catch {
    return false;
  }
}

export function clearDocumentDraft(userId: string, vault: string): void {
  if (!userId) return;
  try {
    window.localStorage.removeItem(documentDraftStorageKey(userId, vault));
    notifyDraftsChanged();
  } catch {
    // Storage can be unavailable in hardened/private browsing contexts. The
    // in-memory composer remains usable and its close confirmation still
    // protects the current session.
  }
}

export interface WorkspaceDraft {
  kind: "new" | "edit";
  vault: string;
  document?: string;
  collection?: string;
  title: string;
  updatedAt: string;
}

export function workspaceDraftHref(draft: WorkspaceDraft): string {
  const base = `/vault/${encodeURIComponent(draft.vault)}/doc/`;
  if (draft.kind === "new") return `${base}new`;
  const identity = draft.document ?? "";
  const parsed = parseDocUri(identity);
  if (identity.startsWith("akb://") && (!parsed || parsed.vault !== draft.vault)) return `/vault/${encodeURIComponent(draft.vault)}`;
  const ref = parsed ? parsed.id : identity.startsWith(`${draft.vault}:`)
    ? identity.slice(draft.vault.length + 1)
    : identity;
  return `${base}${encodeURIComponent(ref)}?view=edit`;
}

/** Metadata only: recovery still goes through each editor's validation contract. */
export function listWorkspaceDrafts(userId: string): WorkspaceDraft[] {
  if (!userId) return [];
  try {
    const result: WorkspaceDraft[] = [];
    const editDocuments = new Map<string, StoredDocumentEditDraft>();
    const newPrefix = `${DRAFT_PREFIX}account:${encodeKeyPart(userId)}:`;
    const editPrefix = `${DOCUMENT_EDIT_DRAFT_PREFIX}:${encodeKeyPart(userId)}:`;
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      if (!key) continue;
      if (key.startsWith(newPrefix)) {
        let vault: string;
        try { vault = decodeURIComponent(key.slice(newPrefix.length)); } catch { continue; }
        const draft = loadDocumentDraft(userId, vault);
        if (draft) result.push({ kind: "new", vault, collection: draft.collection, title: draft.title, updatedAt: draft.updatedAt });
      } else if (key.startsWith(editPrefix)) {
        let candidate: unknown;
        try { candidate = JSON.parse(window.localStorage.getItem(key) || "null"); } catch { continue; }
        if (!recordLooksLikeEditDraft(candidate) || candidate.userId !== userId) continue;
        if (key !== documentEditDraftStorageKey(userId, candidate.vault, candidate.document, candidate.tabId, candidate.draftId)) continue;
        editDocuments.set(JSON.stringify([candidate.vault, candidate.document]), candidate);
      }
    }
    for (const { vault, document } of editDocuments.values()) {
      // Same-tab priority must match the editor, even if another tab is newer.
      const recovered = loadDocumentEditDraft(userId, vault, document);
      if (recovered.status !== "restored") continue;
      if (document.startsWith("akb://")) {
        const parsed = parseDocUri(document);
        if (!parsed || parsed.vault !== vault) continue;
      }
      result.push({ kind: "edit", vault, document, title: recovered.draft.title, updatedAt: recovered.draft.updatedAt });
    }
    return result.sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt));
  } catch {
    return [];
  }
}
