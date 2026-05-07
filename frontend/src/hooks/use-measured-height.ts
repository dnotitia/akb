import { useEffect, useState } from "react";

/**
 * Track an element's rendered height. Returns [setRef, height]. Uses
 * ResizeObserver so it stays accurate across font load, media queries, etc.
 * Height is 0 until the element mounts.
 */
export function useMeasuredHeight(): [(el: HTMLElement | null) => void, number] {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const [height, setHeight] = useState(0);

  useEffect(() => {
    if (!el) return;
    setHeight(el.getBoundingClientRect().height);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) setHeight(entry.contentRect.height);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);

  return [setEl, height];
}
