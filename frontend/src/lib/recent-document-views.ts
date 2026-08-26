const STORAGE_PREFIX = "akb.recentDocumentViews.v1";
const STORED_VIEW_LIMIT = 12;

export interface RecentDocumentView {
  vault: string;
  path: string;
  title: string;
  type: string;
  viewedAt: string;
  updatedAt?: string;
}

export interface RecordRecentDocumentViewInput {
  vault: string;
  path: string;
  title: string;
  type?: string | null;
  updatedAt?: string | null;
}

function storageKey(userId: string): string {
  return `${STORAGE_PREFIX}:${userId}`;
}

function isRecentDocumentView(value: unknown): value is RecentDocumentView {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.vault === "string" && item.vault.length > 0 &&
    typeof item.path === "string" && item.path.length > 0 &&
    typeof item.title === "string" && item.title.length > 0 &&
    typeof item.type === "string" && item.type.length > 0 &&
    typeof item.viewedAt === "string" && Number.isFinite(Date.parse(item.viewedAt)) &&
    (item.updatedAt === undefined ||
      (typeof item.updatedAt === "string" && Number.isFinite(Date.parse(item.updatedAt))))
  );
}

export function readRecentDocumentViews(
  userId: string,
  limit = STORED_VIEW_LIMIT,
): RecentDocumentView[] {
  if (!userId || typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(storageKey(userId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(isRecentDocumentView)
      .sort((a, b) => Date.parse(b.viewedAt) - Date.parse(a.viewedAt))
      .slice(0, Math.max(0, limit));
  } catch {
    // Private browsing policies and corrupted client state both degrade to an
    // absent personal history; document reading itself must remain unaffected.
    return [];
  }
}

export function recordRecentDocumentView(
  userId: string,
  input: RecordRecentDocumentViewInput,
): void {
  if (!userId || typeof window === "undefined") return;
  const vault = input.vault.trim();
  const path = input.path.trim();
  const title = input.title.trim();
  if (!vault || !path || !title) return;

  const next: RecentDocumentView = {
    vault,
    path,
    title,
    type: input.type?.trim() || "note",
    viewedAt: new Date().toISOString(),
    ...(input.updatedAt && Number.isFinite(Date.parse(input.updatedAt))
      ? { updatedAt: input.updatedAt }
      : {}),
  };

  try {
    const current = readRecentDocumentViews(userId);
    const deduplicated = current.filter(
      (item) => item.vault !== vault || item.path !== path,
    );
    window.localStorage.setItem(
      storageKey(userId),
      JSON.stringify([next, ...deduplicated].slice(0, STORED_VIEW_LIMIT)),
    );
  } catch {
    // Recent views are progressive enhancement. Storage denial must never
    // interrupt or report failure in the primary document-reading workflow.
  }
}
