import { useLayoutEffect, useState, type RefObject } from "react";

/** One measured pass for all controls; no observer or layout reads per heading component. */
export function useReaderSectionControls(rootRef: RefObject<HTMLDivElement | null>, body: string) {
  const [positions, setPositions] = useState<Record<number, { left: number; top: number }>>({});
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    let frame = 0;
    const measure = () => {
      frame = 0;
      const origin = root.getBoundingClientRect(), scale = root.offsetWidth ? origin.width / root.offsetWidth : 1;
      const touch = matchMedia("(pointer: coarse), (max-width: 40rem)").matches;
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      const size = (touch ? 2.75 : 1.5) * rem;
      const next: typeof positions = {};
      for (const heading of root.querySelectorAll<HTMLElement>("[data-reading-section-index]")) {
        if (heading.hidden) continue;
        const bounds = heading.getBoundingClientRect(), line = parseFloat(getComputedStyle(heading).lineHeight) || size;
        const left = (bounds.left - origin.left) / scale - size - 2;
        next[Number(heading.dataset.readingSectionIndex)] = { left: touch || left < 0 ? (bounds.right - origin.left) / scale - size : left, top: (bounds.top - origin.top) / scale + (line - size) / 2 };
      }
      setPositions(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure); };
    measure(); const resize = new ResizeObserver(schedule); resize.observe(root);
    const mutations = new MutationObserver(schedule); mutations.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "data-reading-section-index"] });
    root.addEventListener("load", schedule, true); window.addEventListener("resize", schedule);
    return () => { cancelAnimationFrame(frame); resize.disconnect(); mutations.disconnect(); root.removeEventListener("load", schedule, true); window.removeEventListener("resize", schedule); };
  }, [rootRef, body]);
  return positions;
}
