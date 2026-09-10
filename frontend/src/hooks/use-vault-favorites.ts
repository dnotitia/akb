import { useCallback, useEffect, useState } from "react";
import { useCurrentUser } from "@/contexts/current-user-context";

// Per-browser favorited vault IDs. Uses the same quota/disabled-storage-safe
// localStorage idiom as use-graph-history.ts. Keyed by vault.id (the stable PK
// on every /my/vaults row) — NEVER name, which is user-renamable and would
// silently drop a favorite after a rename. (VaultSummary has an unused
// `is_pinned?` field reserved for a future server-synced upgrade; nothing
// populates it today and this hook ignores it.)

const PREFIX = "akb-vault-favorites:v2:";
const EVENT = "akb:vault-favorites-changed";
const memory = new Map<string, string[]>();
const MAX = 100; // defensive cap so a runaway list can't bloat localStorage

/** Unowned legacy IDs are only imported after an explicit account review. */
export function readLegacyVaultFavorites(): string[] {
  return readIds("akb-vault-favorites");
}

function readIds(key: string): string[] {
  if (!key) return [];
  if (memory.has(key)) return memory.get(key)!;
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string").slice(0, MAX);
  } catch {
    return [];
  }
}

function writeIds(key: string, ids: string[]): void {
  try {
    localStorage.setItem(key, JSON.stringify(ids.slice(0, MAX)));
    memory.delete(key);
  } catch {
    memory.set(key, ids);
    // Quota exceeded or storage disabled (private mode) — degrade to the
    // in-memory state already set; never throw and crash the rail.
  }
}

export function useVaultFavorites() {
  const user = useCurrentUser();
  const key = user?.user_id ? `${PREFIX}${encodeURIComponent(user.user_id)}` : "";
  const [state, setState] = useState(() => ({ key, ids: readIds(key) }));
  const favorites = state.key === key ? state.ids : readIds(key);

  // Reconcile across tabs: a pin/unpin in one tab updates the others.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === key || e.key === null) {
        memory.delete(key);
        setState({ key, ids: readIds(key) });
      }
    };
    const onChange = (e: Event) => {
      if ((e as CustomEvent<string>).detail === key) setState({ key, ids: readIds(key) });
    };
    setState({ key, ids: readIds(key) });
    window.addEventListener("storage", onStorage);
    window.addEventListener(EVENT, onChange);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(EVENT, onChange);
    };
  }, [key]);

  const toggleFavorite = useCallback((id: string) => {
      if (!key) return;
      const prev = readIds(key);
      // Newest-favorited floats to the top of the Favorites group.
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [id, ...prev].slice(0, MAX);
      writeIds(key, next);
      window.dispatchEvent(new CustomEvent(EVENT, { detail: key }));
  }, [key]);

  const isFavorite = useCallback((id: string) => favorites.includes(id), [favorites]);
  const importFavorites = useCallback((accessibleIds: string[]) => {
    if (!key) return;
    const legacy = readLegacyVaultFavorites();
    const allowed = new Set(accessibleIds);
    const next = [...new Set([...readIds(key), ...legacy.filter(id => allowed.has(id))])].slice(0, MAX);
    writeIds(key, next);
    window.dispatchEvent(new CustomEvent(EVENT, { detail: key }));
  }, [key]);
  /** Position within the favorites order — used to sort the Favorites group. */
  const favOrder = useCallback((id: string) => favorites.indexOf(id), [favorites]);

  return { favorites, isFavorite, toggleFavorite, favOrder, importFavorites };
}
