// Shared filtering helpers for menu/picker surfaces. Kept in a non-component
// module so the `MenuFilter` component file stays Fast-Refresh clean (a file
// that exports a component must not also export constants/functions).

/**
 * Show a filter box atop a menu/picker once the list grows past this many items
 * — a filter only earns its keep when the list is long enough to scan. Shared so
 * every vault/option picker triggers at the same length.
 */
export const MENU_FILTER_THRESHOLD = 7;

/** Case-insensitive substring filter over an arbitrary list by a text accessor. */
export function filterByText<T>(items: T[], query: string, getText: (item: T) => string): T[] {
  const q = query.trim().toLowerCase();
  if (!q) return items;
  return items.filter((it) => getText(it).toLowerCase().includes(q));
}
