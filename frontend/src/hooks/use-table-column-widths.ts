import { useCallback, useEffect, useState } from "react";

/** The narrowest a person can make a table column, in CSS pixels. */
export const TABLE_COLUMN_MIN_WIDTH = 80;
/** The widest a person can make a table column, in CSS pixels. */
export const TABLE_COLUMN_MAX_WIDTH = 960;

const STORAGE_PREFIX = "akb.tableColumnWidths.v1:";

/** Where a table's column widths are kept in this browser. */
export function tableColumnWidthsStorageKey(vault: string, table: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(vault)}/${encodeURIComponent(table)}`;
}

/** A width in whole pixels within the allowed range. */
export function clampTableColumnWidth(px: number): number {
  return Math.min(TABLE_COLUMN_MAX_WIDTH, Math.max(TABLE_COLUMN_MIN_WIDTH, Math.round(px)));
}

export interface TableColumnWidths {
  /**
   * Widths chosen for this table's columns, by column name. A column without
   * an entry keeps its automatic width. A Map, so a column named like an
   * object property ("constructor", "__proto__") is an ordinary entry.
   */
  widths: ReadonlyMap<string, number>;
  /** Gives the column a fixed width, clamped to the allowed range. */
  setWidth: (column: string, px: number) => void;
  /** Returns the column to its automatic width. */
  resetWidth: (column: string) => void;
}

interface WidthsState {
  storageKey: string | null;
  widths: ReadonlyMap<string, number>;
  /** Set by a person's change, so loading the saved widths never writes them back. */
  changed: boolean;
}

const NO_WIDTHS: ReadonlyMap<string, number> = new Map();

/**
 * The column widths a person chose for one table, remembered per table in
 * this browser. Storage is optional: when it is unavailable or full, widths
 * still apply and last until the page unmounts. Saved entries that are not
 * finite numbers within the allowed range are ignored.
 */
export function useTableColumnWidths(
  vault: string | undefined,
  table: string | undefined,
): TableColumnWidths {
  const storageKey = vault && table ? tableColumnWidthsStorageKey(vault, table) : null;
  const [state, setState] = useState<WidthsState>(() => ({
    storageKey,
    widths: readWidths(storageKey),
    changed: false,
  }));
  // Another table loads its own widths.
  let current = state;
  if (current.storageKey !== storageKey) {
    current = { storageKey, widths: readWidths(storageKey), changed: false };
    setState(current);
  }

  useEffect(() => {
    if (state.changed) writeWidths(state.storageKey, state.widths);
  }, [state]);

  const setWidth = useCallback((column: string, px: number) => {
    if (!Number.isFinite(px)) return;
    const width = clampTableColumnWidth(px);
    setState((previous) => {
      if (previous.widths.get(column) === width) return previous;
      const widths = new Map(previous.widths);
      widths.set(column, width);
      return { ...previous, widths, changed: true };
    });
  }, []);

  const resetWidth = useCallback((column: string) => {
    setState((previous) => {
      if (!previous.widths.has(column)) return previous;
      const widths = new Map(previous.widths);
      widths.delete(column);
      return { ...previous, widths, changed: true };
    });
  }, []);

  return { widths: current.widths, setWidth, resetWidth };
}

function readWidths(storageKey: string | null): ReadonlyMap<string, number> {
  if (!storageKey) return NO_WIDTHS;
  let saved: unknown;
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return NO_WIDTHS;
    saved = JSON.parse(raw);
  } catch {
    return NO_WIDTHS;
  }
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) return NO_WIDTHS;
  const widths = new Map<string, number>();
  for (const [column, width] of Object.entries(saved)) {
    if (
      typeof width === "number" &&
      Number.isFinite(width) &&
      width >= TABLE_COLUMN_MIN_WIDTH &&
      width <= TABLE_COLUMN_MAX_WIDTH
    ) {
      widths.set(column, width);
    }
  }
  return widths;
}

function writeWidths(storageKey: string | null, widths: ReadonlyMap<string, number>) {
  if (!storageKey) return;
  try {
    if (widths.size === 0) localStorage.removeItem(storageKey);
    else localStorage.setItem(storageKey, JSON.stringify(Object.fromEntries(widths)));
  } catch {
    /* Storage is optional: the widths then last until the page unmounts. */
  }
}
