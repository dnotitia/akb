import { useEffect, useMemo, useRef, useState } from "react";
import { parseHeadings, type Heading } from "@/lib/markdown";

interface Options {
  /** Root element whose descendant headings should be scroll-spied. */
  root?: HTMLElement | null;
  /** Extra top offset (sticky header, etc.) when deciding active heading. */
  topOffset?: number;
}

/**
 * Parse headings from markdown for a TOC + track which one is currently in
 * view so the outline can highlight it. We track the *last heading above
 * the threshold* — readers expect the current section to stay active even
 * when no heading is on-screen, which a naive IntersectionObserver
 * ("first visible") can't express.
 *
 * The scroll-spy listens on the nearest scrollable ancestor of `root`
 * (falling back to window) so it also works when content lives inside
 * an overflow-y container.
 */
export function useDocOutline(md: string, { root, topOffset = 96 }: Options = {}) {
  const headings = useMemo(() => parseHeadings(md), [md]);
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const frame = useRef<number | null>(null);

  useEffect(() => {
    if (!root || headings.length === 0) {
      setActiveSlug(null);
      return;
    }

    const scroller = findScrollParent(root);
    const threshold = topOffset + 8;

    const getTopOf = (el: HTMLElement): number => {
      if (scroller === window) return el.getBoundingClientRect().top;
      const scrollerRect = (scroller as HTMLElement).getBoundingClientRect();
      return el.getBoundingClientRect().top - scrollerRect.top;
    };

    const compute = () => {
      frame.current = null;
      const nodes = headings
        .map((h) => root.querySelector<HTMLElement>(`#${cssEscape(h.slug)}`))
        .filter((el): el is HTMLElement => !!el);
      if (nodes.length === 0) return;
      let current: HTMLElement | null = nodes[0];
      for (const n of nodes) {
        if (getTopOf(n) - threshold <= 0) current = n;
        else break;
      }
      setActiveSlug(current?.id ?? null);
    };

    const onScroll = () => {
      if (frame.current != null) return;
      frame.current = requestAnimationFrame(compute);
    };

    compute();
    const target: EventTarget = scroller;
    target.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      target.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (frame.current != null) cancelAnimationFrame(frame.current);
    };
  }, [root, headings, topOffset]);

  return { headings, activeSlug };
}

/** Find the closest ancestor that actually scrolls, or `window` as a fallback. */
function findScrollParent(el: HTMLElement): HTMLElement | Window {
  let node: HTMLElement | null = el.parentElement;
  while (node) {
    const s = getComputedStyle(node);
    if (/(auto|scroll|overlay)/.test(s.overflowY) && node.scrollHeight > node.clientHeight) {
      return node;
    }
    node = node.parentElement;
  }
  return window;
}

function cssEscape(s: string): string {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(s);
  return s.replace(/([^\w-])/g, "\\$1");
}

export type { Heading };
