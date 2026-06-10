import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Pointer-drag resize for a fixed-width grid column. Width persists
 * across sessions in localStorage and is clamped to [min, max] so the
 * column can never collapse a neighbour or overrun the viewport.
 *
 * Spread `handlers` onto the drag handle element. The handle captures
 * the pointer on press so the drag keeps tracking even when the cursor
 * leaves the thin hit area, and locks body cursor/selection for the
 * duration. Double-clicking the handle resets to `default`.
 *
 * Used by VaultShell to resize the collection-tree column (the vault rail
 * to its left is fixed-width).
 */
export interface ColumnResizeOptions {
  storageKey: string;
  min: number;
  max: number;
  /** Width used before any user drag (and on double-click reset). */
  default: number;
}

export interface ColumnResize {
  width: number;
  setWidth: (w: number) => void;
  reset: () => void;
  handlers: {
    onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => void;
    onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => void;
    onPointerUp: (e: React.PointerEvent<HTMLDivElement>) => void;
    onPointerCancel: (e: React.PointerEvent<HTMLDivElement>) => void;
    onDoubleClick: () => void;
  };
}

export function useColumnResize({
  storageKey,
  min,
  max,
  default: def,
}: ColumnResizeOptions): ColumnResize {
  const load = (): number => {
    if (typeof window === "undefined") return def;
    const saved = Number(window.localStorage.getItem(storageKey));
    return Number.isFinite(saved) && saved >= min && saved <= max ? saved : def;
  };
  const [width, setWidth] = useState<number>(load);

  // Persist the chosen width so it survives reloads and vault switches.
  useEffect(() => {
    window.localStorage.setItem(storageKey, String(width));
  }, [storageKey, width]);

  const dragRef = useRef<{ startX: number; startW: number } | null>(null);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      e.preventDefault();
      dragRef.current = { startX: e.clientX, startW: width };
      e.currentTarget.setPointerCapture(e.pointerId);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [width],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (!drag) return;
      setWidth(
        Math.min(max, Math.max(min, drag.startW + (e.clientX - drag.startX))),
      );
    },
    [min, max],
  );

  const onPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  const reset = useCallback(() => setWidth(def), [def]);

  return {
    width,
    setWidth,
    reset,
    handlers: {
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onPointerCancel: onPointerUp,
      onDoubleClick: reset,
    },
  };
}
