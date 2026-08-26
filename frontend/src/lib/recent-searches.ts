const STORAGE_KEY_PREFIX = "akb.recentSearches.v1";
const MAX_STORED_SEARCHES = 8;

export type RecentSearchMode = "semantic" | "literal";
export type RecentSearchSurface = "global" | "advanced";

export interface RecentSearch {
  query: string;
  mode: RecentSearchMode;
  vaults: string[];
  searchedAt: string;
  surface: RecentSearchSurface;
}

export interface RecordRecentSearchInput {
  query: string;
  mode: RecentSearchMode;
  vaults?: string[];
  surface: RecentSearchSurface;
}

function storageKey(userId: string): string {
  return `${STORAGE_KEY_PREFIX}:${userId}`;
}

function isRecentSearch(value: unknown): value is RecentSearch {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<RecentSearch>;
  return (
    typeof item.query === "string" &&
    item.query.trim().length > 0 &&
    (item.mode === "semantic" || item.mode === "literal") &&
    Array.isArray(item.vaults) &&
    item.vaults.every((vault) => typeof vault === "string") &&
    typeof item.searchedAt === "string" &&
    !Number.isNaN(Date.parse(item.searchedAt)) &&
    (item.surface === "global" || item.surface === "advanced")
  );
}

export function readRecentSearches(
  userId: string,
  limit = MAX_STORED_SEARCHES,
): RecentSearch[] {
  if (!userId || typeof window === "undefined") return [];

  try {
    const raw = window.localStorage.getItem(storageKey(userId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(isRecentSearch)
      .sort(
        (left, right) =>
          Date.parse(right.searchedAt) - Date.parse(left.searchedAt),
      )
      .slice(0, Math.max(0, limit));
  } catch {
    return [];
  }
}

export function recordRecentSearch(
  userId: string,
  input: RecordRecentSearchInput,
): void {
  const query = input.query.trim();
  if (!userId || !query || typeof window === "undefined") return;

  const vaults = [...new Set(input.vaults ?? [])]
    .map((vault) => vault.trim())
    .filter(Boolean)
    .sort((left, right) => left.localeCompare(right));
  const identity = `${input.surface}:${input.mode}:${vaults.join(",")}:${query.toLocaleLowerCase()}`;
  const nextItem: RecentSearch = {
    query,
    mode: input.mode,
    vaults,
    searchedAt: new Date().toISOString(),
    surface: input.surface,
  };

  try {
    const remaining = readRecentSearches(userId).filter((item) => {
      const itemIdentity = `${item.surface}:${item.mode}:${item.vaults.join(",")}:${item.query.toLocaleLowerCase()}`;
      return itemIdentity !== identity;
    });
    window.localStorage.setItem(
      storageKey(userId),
      JSON.stringify([nextItem, ...remaining].slice(0, MAX_STORED_SEARCHES)),
    );
  } catch {
    // Search history is a local enhancement and must never interrupt search.
  }
}

export function clearRecentSearches(userId: string): void {
  if (!userId || typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(storageKey(userId));
  } catch {
    // A denied storage write should not affect the search workspace.
  }
}
