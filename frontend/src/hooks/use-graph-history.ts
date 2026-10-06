// frontend/src/hooks/use-graph-history.ts
import { useCallback, useEffect, useState } from "react";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";

export interface RecentEntry {
  doc_id: string;
  title: string;
  kind?: "document" | "table" | "file";
  uri?: string;
}

export interface SavedView {
  name: string;
  url: string;
}

const RECENT_MAX = 5;
const SAVED_MAX = 20;

const recentKey = (scope: string) => `akb-graph-recent:v2:${scope}`;
const savedKey = (scope: string) => `akb-graph-saves:v2:${scope}`;

function readEntries<T>(key: string, valid: (value: unknown) => value is T, limit: number, fallback: T[] = []): T[] {
  try {
    const raw = localStorage.getItem(key);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter(valid).slice(0, limit) : [];
  } catch {
    return fallback;
  }
}

function isRecent(value: unknown): value is RecentEntry {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<RecentEntry>;
  return typeof item.doc_id === "string" && typeof item.title === "string" &&
    (item.uri === undefined || typeof item.uri === "string") &&
    (item.kind === undefined || ["document", "table", "file"].includes(item.kind));
}

function isSaved(value: unknown): value is SavedView {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<SavedView>;
  return typeof item.name === "string" && typeof item.url === "string" && item.url.startsWith("?");
}

function writeJson(key: string, value: unknown): boolean {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    // Quota exceeded or storage disabled — caller may surface a toast.
    return false;
  }
}

export function useGraphHistory(vault: string) {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const scope = user?.user_id ? `${encodeURIComponent(user.user_id)}:${encodeURIComponent(vault)}` : "";
  const read = useCallback((fallback?: { recent: RecentEntry[]; saved: SavedView[] }) => ({
    scope, revision,
    recent: scope ? readEntries(recentKey(scope), isRecent, RECENT_MAX, fallback?.recent) : [],
    saved: scope ? readEntries(savedKey(scope), isSaved, SAVED_MAX, fallback?.saved) : [],
  }), [scope, revision]);
  const [state, setState] = useState(read);
  // Do not expose another account's values for even the render before effects.
  const current = state.scope === scope ? state : read();
  const verified = !checking && current.revision === revision;
  const recent = verified ? current.recent : [];
  const saved = verified ? current.saved : [];

  useEffect(() => {
    setState((prev) => {
      if (prev.scope !== scope) return read();
      if (prev.revision === revision) return prev;
      // Foreground verification is not a deletion request. Resource suggestions
      // are resolved against the current loaded scene by the toolbar.
      return read(prev);
    });
  }, [scope, revision, read]);

  const pushRecent = useCallback(
    (entry: RecentEntry) => {
      if (!scope || checking) return;
      setState((prev) => {
        const current = prev.scope === scope && prev.revision === revision ? prev : read();
        const filtered = current.recent.filter((r) => (r.uri || r.doc_id) !== (entry.uri || entry.doc_id));
        const next = [entry, ...filtered].slice(0, RECENT_MAX);
        writeJson(recentKey(scope), next);
        return { ...current, recent: next };
      });
    },
    [scope, checking, revision, read],
  );

  const clearRecent = useCallback(() => {
    if (!scope || checking) return;
    writeJson(recentKey(scope), []);
    setState((prev) => ({ ...(prev.scope === scope ? prev : read()), recent: [] }));
  }, [scope, checking, read]);

  const saveView = useCallback(
    (name: string, url: string) => {
      if (!scope || checking) return;
      setState((prev) => {
        const current = prev.scope === scope && prev.revision === revision ? prev : read();
        const filtered = current.saved.filter((v) => v.name !== name);
        const next = [{ name, url }, ...filtered].slice(0, SAVED_MAX);
        writeJson(savedKey(scope), next);
        return { ...current, saved: next };
      });
    },
    [scope, checking, revision, read],
  );

  const deleteView = useCallback(
    (name: string) => {
      if (!scope || checking) return;
      setState((prev) => {
        const current = prev.scope === scope && prev.revision === revision ? prev : read();
        const next = current.saved.filter((v) => v.name !== name);
        writeJson(savedKey(scope), next);
        return { ...current, saved: next };
      });
    },
    [scope, checking, revision, read],
  );

  return { recent, pushRecent, clearRecent, saved, saveView, deleteView };
}
