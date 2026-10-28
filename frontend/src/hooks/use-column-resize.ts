import { useCallback, useEffect, useRef, useState } from "react";

/** A persisted, bounded column width with pointer and keyboard resizing. */
export interface ColumnResizeOptions {
  storageKey: string;
  min: number;
  max: number;
  /** Width used before any user drag (and on double-click reset). */
  default: number;
}

export interface ColumnResize {
  width: number;
  /** Responsive layout decisions wait for the active drag to finish. */
  settledWidth: number;
  isResizing: boolean;
  setWidth: (w: number) => void;
  reset: () => void;
  cancel: () => void;
  handlers: {
    onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => void;
    onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => void;
    onPointerUp: (e: React.PointerEvent<HTMLDivElement>) => void;
    onPointerCancel: (e: React.PointerEvent<HTMLDivElement>) => void;
    onLostPointerCapture: (e: React.PointerEvent<HTMLDivElement>) => void;
    onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => void;
    onDoubleClick: () => void;
  };
}

export function useColumnResize({ storageKey, min, max, default: def }: ColumnResizeOptions): ColumnResize {
  const clamp = useCallback((value: number) => Math.min(max, Math.max(min, value)), [min, max]);
  const [width, updateWidth] = useState(() => {
    if (typeof window === "undefined") return def;
    const saved = Number(window.localStorage.getItem(storageKey));
    return Number.isFinite(saved) && saved >= min && saved <= max ? saved : def;
  });
  const [dragStartWidth, setDragStartWidth] = useState<number | null>(null);
  const dragRef = useRef<{
    startX: number;
    startW: number;
    pointerId: number;
    target: HTMLDivElement;
    cursor: string;
    userSelect: string;
  } | null>(null);

  useEffect(() => {
    window.localStorage.setItem(storageKey, String(width));
  }, [storageKey, width]);

  const finishDrag = useCallback((updateState = true) => {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    if (drag.target.hasPointerCapture(drag.pointerId)) {
      drag.target.releasePointerCapture(drag.pointerId);
    }
    document.body.style.cursor = drag.cursor;
    document.body.style.userSelect = drag.userSelect;
    if (updateState) setDragStartWidth(null);
  }, []);

  useEffect(() => {
    const cancel = () => finishDrag();
    window.addEventListener("blur", cancel);
    return () => {
      window.removeEventListener("blur", cancel);
      finishDrag(false);
    };
  }, [finishDrag]);

  const onPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || event.isPrimary === false || dragRef.current) return;
    event.preventDefault();
    event.currentTarget.focus();
    dragRef.current = {
      startX: event.clientX, startW: width, pointerId: event.pointerId,
      target: event.currentTarget,
      cursor: document.body.style.cursor, userSelect: document.body.style.userSelect,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragStartWidth(width);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, [width]);

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    updateWidth(clamp(drag.startW + event.clientX - drag.startX));
  }, [clamp]);

  const onPointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) finishDrag();
  }, [finishDrag]);
  const cancel = useCallback(() => finishDrag(), [finishDrag]);
  const setWidth = useCallback((value: number) => updateWidth(clamp(value)), [clamp]);
  const reset = useCallback(() => setWidth(def), [def, setWidth]);
  const onKeyDown = useCallback((event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 32 : 8;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      updateWidth(current => clamp(current + (event.key === "ArrowLeft" ? -step : step)));
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      updateWidth(event.key === "Home" ? min : max);
    }
  }, [clamp, min, max]);

  return {
    width, settledWidth: dragStartWidth ?? width, isResizing: dragStartWidth !== null,
    setWidth, reset, cancel,
    handlers: {
      onPointerDown, onPointerMove, onPointerUp,
      onPointerCancel: onPointerUp, onLostPointerCapture: onPointerUp,
      onKeyDown, onDoubleClick: reset,
    },
  };
}
