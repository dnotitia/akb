import { useEffect, useState } from "react";

export interface RecentCollection {
  vault: string;
  path: string;
  viewedAt: string;
}

export const RECENT_COLLECTIONS_EVENT = "akb:recent-collections-changed";
const LIMIT = 12;
const storageKey = (userId: string) => `akb.recentCollections.v1:${userId}`;
const identity = ({ vault, path }: Pick<RecentCollection, "vault" | "path">) => JSON.stringify([vault, path]);
function validName(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 2048;
}
function valid(value: unknown): value is RecentCollection {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  if (!validName(item.vault) || !validName(item.path) || typeof item.viewedAt !== "string" || item.viewedAt.length > 32) return false;
  const time = Date.parse(item.viewedAt);
  return Number.isFinite(time) && time >= 0 && time <= Date.now() && new Date(time).toISOString() === item.viewedAt;
}

export function readRecentCollections(userId: string, limit = LIMIT): RecentCollection[] {
  if (!userId || typeof window === "undefined") return [];
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(storageKey(userId)) || "[]");
    if (!Array.isArray(parsed)) return [];
    const seen = new Set<string>();
    return parsed.filter(valid).sort((a, b) => Date.parse(b.viewedAt) - Date.parse(a.viewedAt)).filter(item => {
      const id = identity(item);
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    }).slice(0, Number.isFinite(limit) ? Math.min(LIMIT, Math.max(0, Math.floor(limit))) : LIMIT)
      .map(({ vault, path, viewedAt }) => ({ vault, path, viewedAt }));
  } catch { return []; }
}

export function recordRecentCollection(userId: string, input: Pick<RecentCollection, "vault" | "path">): void {
  if (!userId || typeof window === "undefined" || !validName(input.vault) || !validName(input.path)) return;
  const next: RecentCollection = { vault: input.vault, path: input.path, viewedAt: new Date().toISOString() };
  try {
    const current = readRecentCollections(userId).filter(item => identity(item) !== identity(next));
    window.localStorage.setItem(storageKey(userId), JSON.stringify([next, ...current].slice(0, LIMIT)));
    window.dispatchEvent(new CustomEvent(RECENT_COLLECTIONS_EVENT, { detail: userId }));
  } catch { /* Optional local history must never interrupt navigation. */ }
}

export function useRecentCollections(userId: string | undefined): RecentCollection[] {
  const [, refresh] = useState(0);
  useEffect(() => {
    const onChange = (event: Event) => {
      if ((event as CustomEvent<string>).detail === userId) refresh(value => value + 1);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === storageKey(userId || "")) refresh(value => value + 1);
    };
    window.addEventListener(RECENT_COLLECTIONS_EVENT, onChange);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(RECENT_COLLECTIONS_EVENT, onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, [userId]);
  return readRecentCollections(userId || "");
}
