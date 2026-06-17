import { useCallback, useEffect, useState } from "react";

// Per-browser favorited vault IDs. Uses the same quota/disabled-storage-safe
// localStorage idiom as use-graph-history.ts. Keyed by vault.id (the stable PK
// on every /my/vaults row) — NEVER name, which is user-renamable and would
// silently drop a favorite after a rename. (VaultSummary has an unused
// `is_pinned?` field reserved for a future server-synced upgrade; nothing
// populates it today and this hook ignores it.)

const KEY = "akb-vault-favorites";
const MAX = 100; // defensive cap so a runaway list can't bloat localStorage

function readIds(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string").slice(0, MAX);
  } catch {
    return [];
  }
}

function writeIds(ids: string[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(ids.slice(0, MAX)));
  } catch {
    // Quota exceeded or storage disabled (private mode) — degrade to the
    // in-memory state already set; never throw and crash the rail.
  }
}

export function useVaultFavorites() {
  const [favorites, setFavorites] = useState<string[]>(readIds);

  // Reconcile across tabs: a pin/unpin in one tab updates the others.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === KEY) setFavorites(readIds());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const toggleFavorite = useCallback((id: string) => {
    setFavorites((prev) => {
      // Newest-favorited floats to the top of the Favorites group.
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [id, ...prev].slice(0, MAX);
      writeIds(next);
      return next;
    });
  }, []);

  const isFavorite = useCallback((id: string) => favorites.includes(id), [favorites]);
  /** Position within the favorites order — used to sort the Favorites group. */
  const favOrder = useCallback((id: string) => favorites.indexOf(id), [favorites]);

  return { favorites, isFavorite, toggleFavorite, favOrder };
}
