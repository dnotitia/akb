// frontend/src/hooks/use-graph-history.ts
import { useCallback, useEffect, useState } from "react";

export interface RecentEntry {
  doc_id: string;
  title: string;
}

export interface SavedView {
  name: string;
  url: string;
}

const RECENT_MAX = 5;
const SAVED_MAX = 20;

const recentKey = (vault: string) => `akb-graph-recent:${vault}`;
const savedKey = (vault: string) => `akb-graph-saves:${vault}`;

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
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
  const [recent, setRecent] = useState<RecentEntry[]>(() => readJson(recentKey(vault), []));
  const [saved, setSaved] = useState<SavedView[]>(() => readJson(savedKey(vault), []));

  useEffect(() => {
    setRecent(readJson(recentKey(vault), []));
    setSaved(readJson(savedKey(vault), []));
  }, [vault]);

  const pushRecent = useCallback(
    (entry: RecentEntry) => {
      setRecent((prev) => {
        const filtered = prev.filter((r) => r.doc_id !== entry.doc_id);
        const next = [entry, ...filtered].slice(0, RECENT_MAX);
        writeJson(recentKey(vault), next);
        return next;
      });
    },
    [vault],
  );

  const clearRecent = useCallback(() => {
    writeJson(recentKey(vault), []);
    setRecent([]);
  }, [vault]);

  const saveView = useCallback(
    (name: string, url: string) => {
      setSaved((prev) => {
        const filtered = prev.filter((v) => v.name !== name);
        const next = [{ name, url }, ...filtered].slice(0, SAVED_MAX);
        writeJson(savedKey(vault), next);
        return next;
      });
    },
    [vault],
  );

  const deleteView = useCallback(
    (name: string) => {
      setSaved((prev) => {
        const next = prev.filter((v) => v.name !== name);
        writeJson(savedKey(vault), next);
        return next;
      });
    },
    [vault],
  );

  return { recent, pushRecent, clearRecent, saved, saveView, deleteView };
}
