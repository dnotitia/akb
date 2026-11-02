import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent, type KeyboardEvent, type RefObject } from "react";

const key = "akb:reader-panel-layout:v1";
function initialLayout() {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "null");
    return { width: typeof value?.width === "number" && value.width >= 256 && value.width <= 480 ? value.width : 288,
      ratio: typeof value?.ratio === "number" && value.ratio >= .2 && value.ratio <= .75 ? value.ratio : .5 };
  } catch { return { width: 288, ratio: .5 }; }
}

export function useReaderPanelLayout(frame: RefObject<HTMLDivElement | null>, split: boolean) {
  const [bounds, setBounds] = useState({ width: 0, height: 0, rem: 16 });
  const [layout, setLayout] = useState(initialLayout);
  const drag = useRef<{ target: HTMLDivElement; id: number; x: number; width: number; before: typeof layout } | null>(null);
  const [resizing, setResizing] = useState(false);
  useLayoutEffect(() => {
    const element = frame.current;
    if (!element) return;
    const measure = () => { const rect = element.getBoundingClientRect(); setBounds({ width: rect.width, height: rect.height, rem: parseFloat(getComputedStyle(document.documentElement).fontSize) || 16 }); };
    measure(); const observer = new ResizeObserver(measure); observer.observe(element);
    window.addEventListener("resize", measure);
    return () => { observer.disconnect(); window.removeEventListener("resize", measure); };
  }, [frame]);
  const min = split ? 22 * bounds.rem : 16 * bounds.rem;
  const max = Math.max(min, Math.min(split ? bounds.width - 26 * bounds.rem - 8 : 480, bounds.width - 24 * bounds.rem - 8));
  const width = Math.max(min, Math.min(max, split ? bounds.width * layout.ratio : layout.width));
  const setWidth = useCallback((value: number) => {
    const next = Math.max(min, Math.min(max, value));
    setLayout(previous => split ? { ...previous, ratio: bounds.width ? next / bounds.width : .5 } : { ...previous, width: next });
  }, [bounds.width, min, max, split]);
  const stop = useCallback((cancel = false) => {
    const current = drag.current; if (!current) return;
    drag.current = null;
    if (current.target.hasPointerCapture(current.id)) current.target.releasePointerCapture(current.id);
    if (cancel) setLayout(current.before);
    setResizing(false);
  }, []);
  useEffect(() => {
    const cancel = () => stop(true);
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape" && drag.current) { event.preventDefault(); event.stopPropagation(); stop(true); } };
    window.addEventListener("blur", cancel); window.addEventListener("keydown", escape, true);
    return () => { stop(); window.removeEventListener("blur", cancel); window.removeEventListener("keydown", escape, true); };
  }, [stop]);
  useEffect(() => { if (!resizing) { try { localStorage.setItem(key, JSON.stringify(layout)); } catch { /* Optional view preferences. */ } } }, [layout, resizing]);
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || event.isPrimary === false || drag.current) return;
    event.preventDefault(); event.currentTarget.focus();
    drag.current = { target: event.currentTarget, id: event.pointerId, x: event.clientX, width, before: layout };
    event.currentTarget.setPointerCapture(event.pointerId); setResizing(true);
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => { if (drag.current?.id === event.pointerId) setWidth(drag.current.width - (event.clientX - drag.current.x)); };
  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => { if (drag.current?.id === event.pointerId) stop(); };
  const onPointerCancel = () => stop(true);
  const reset = () => setWidth(split ? bounds.width / 2 : 288);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 32 : 8;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); setWidth(width + (event.key === "ArrowLeft" ? step : -step)); }
    if (event.key === "Home" || event.key === "End") { event.preventDefault(); setWidth(event.key === "Home" ? min : max); }
    if (event.key === "Enter") { event.preventDefault(); reset(); }
  };
  return { width, min, max, resizing, setWidth, reset,
    narrow: bounds.width < 50 * bounds.rem || bounds.height < 20 * bounds.rem,
    splitNarrow: bounds.width < 56 * bounds.rem || bounds.height < 20 * bounds.rem,
    preset: (value: number) => setWidth(split ? bounds.width * value : value),
    handlers: { onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onLostPointerCapture: onPointerCancel, onKeyDown, onDoubleClick: reset } };
}
